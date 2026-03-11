import json
import logging

import aio_pika
from aio_pika import IncomingMessage, Message, DeliveryMode

from dispatcher.job_types import JobType, QUEUE_ROUTING
from workers.mgn_result import MgnResult, MgnStatus
from workers.state_manager_client import StateManagerClient
from workers.aws_clients import get_mgn_client

logger = logging.getLogger(__name__)

MGN_QUEUE = "mgn_jobs"

# Maps each MGN job type to the server state expected when the job arrives.
# If the server is not in this state, the message is stale (duplicate or
# out-of-order) and is acked without doing any work.
VALID_STATES_FOR_JOB: dict[JobType, str] = {
    JobType.ADD_SERVER:                        "PENDING",
    JobType.CONFIGURE_REPLICATION_SETTINGS:    "AGENT_INSTALLED",
    JobType.START_REPLICATION:                 "REPLICATION_STARTED",
    JobType.CONFIGURE_TEST_LAUNCH_TEMPLATE:    "READY_FOR_TESTING",
    JobType.LAUNCH_TEST_INSTANCE:              "TEST_INSTANCE_LAUNCHING",
    JobType.FINALIZE_TEST:                     "TEST_FINALIZED",
    JobType.CONFIGURE_CUTOVER_LAUNCH_TEMPLATE: "CUTOVER_LAUNCH_TEMPLATE_CONFIGURED",
    JobType.START_CUTOVER:                     "CUTOVER_STARTED",
    JobType.LAUNCH_CUTOVER_INSTANCE:           "READY_FOR_CUTOVER_LAUNCH",
    JobType.FINALIZE_CUTOVER:                  "CUTOVER_FINALIZED",
    JobType.DISCONNECT_SOURCE_SERVER:          "DISCONNECTING",
    JobType.ARCHIVE_SOURCE_SERVER:             "ARCHIVED",
    JobType.DELETE_REPLICATION_COMPONENTS:     "CLEANUP_COMPLETE",
}


class MgnWorker:
    """
    Consumes jobs from the mgn_jobs queue and executes AWS MGN and EC2 API
    calls to drive the migration lifecycle forward.

    Each handler makes one atomic AWS API call and returns an MgnResult.
    The message loop in _on_message reads that result and decides whether to
    advance state, dispatch a follow-up poll job, or nack to the DLX.
    """

    def __init__(self, amqp_url: str, state_manager: StateManagerClient):
        self._amqp_url     = amqp_url #to connect to rabbitmq
        self._state_manager = state_manager
        self._connection   = None
        self._channel      = None

        # boto3 client created once at construction — not per-message.
        # Creating a client is not free (credential loading, session setup),
        # so we pay that cost once and reuse across all handler calls.
        self._mgn = get_mgn_client()

        self._handlers: dict[JobType, callable] = {
            JobType.ADD_SERVER:                        self._handle_add_server,
            JobType.CONFIGURE_REPLICATION_SETTINGS:    self._handle_configure_replication,
            JobType.START_REPLICATION:                 self._handle_start_replication,
            JobType.CONFIGURE_TEST_LAUNCH_TEMPLATE:    self._handle_configure_test_launch_template,
            JobType.LAUNCH_TEST_INSTANCE:              self._handle_launch_test_instance,
            JobType.FINALIZE_TEST:                     self._handle_finalize_test,
            JobType.CONFIGURE_CUTOVER_LAUNCH_TEMPLATE: self._handle_configure_cutover_launch_template,
            JobType.START_CUTOVER:                     self._handle_start_cutover,
            JobType.LAUNCH_CUTOVER_INSTANCE:           self._handle_launch_cutover_instance,
            JobType.FINALIZE_CUTOVER:                  self._handle_finalize_cutover,
            JobType.DISCONNECT_SOURCE_SERVER:          self._handle_disconnect_source_server,
            JobType.ARCHIVE_SOURCE_SERVER:             self._handle_archive_source_server,
            JobType.DELETE_REPLICATION_COMPONENTS:     self._handle_delete_replication_components,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self._amqp_url) #pick up the connection to rabbitmq
        self._channel    = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=1)

    async def start(self) -> None:
        #begin listening for jobs
        queue = await self._channel.declare_queue(MGN_QUEUE, passive=True)
        await queue.consume(self._on_message)
        logger.info("[mgn] Listening on %s", MGN_QUEUE)

    async def close(self) -> None:
        if self._connection and not self._connection.is_closed:
            await self._connection.close()

    # ------------------------------------------------------------------
    # Message loop
    # ------------------------------------------------------------------

    async def _on_message(self, message: IncomingMessage) -> None:
        """
        Called by aio-pika for every message from mgn_jobs.

        Same ack/nack pattern as the Poller Worker — exceptions are caught
        here and never allowed to propagate to aio-pika's task wrapper.
        If they did, the channel would destabilise, connect_robust would
        reconnect, and the unacked message would loop indefinitely.
        """
        try:
            body      = json.loads(message.body)
            job_id    = body.get("job_id") or message.message_id or "unknown"
            job_type  = JobType(body["job_type"])
            payload   = body["payload"]
            server_id = payload["server_id"]

            logger.info(
                "[mgn] Received %s for server %s (job_id=%s)",
                job_type.value, server_id, job_id,
            )

            # ---- Stale message check ----
            expected_state = VALID_STATES_FOR_JOB.get(job_type)
            if expected_state is not None:
                current_state = await self._state_manager.get_server_state(server_id)
                if current_state != expected_state:
                    logger.info(
                        "[mgn] Skipping %s for %s — expected state %s, got %s (stale or duplicate)",
                        job_type.value, server_id, expected_state, current_state,
                    )
                    await message.ack()
                    return

            # ---- Handler dispatch ----
            handler = self._handlers.get(job_type)
            if handler is None:
                logger.error("[mgn] No handler for job type: %s — sending to DLX", job_type)
                await message.nack(requeue=False)
                return

            result: MgnResult = await handler(payload) #send the job to the required handler and fetch result

            # ---- Act on result ----
            if result.status == MgnStatus.SUCCESS:
                if result.next_state is not None:
                    logger.info(
                        "[mgn] %s succeeded for %s → advancing to %s",
                        job_type.value, server_id, result.next_state,
                    )
                    await self._state_manager.advance_state(
                        server_id,
                        result.next_state,
                        job_id=job_id,
                        job_type=job_type.value,
                        metadata=result.metadata,
                    )
                else:
                    logger.info(
                        "[mgn] %s succeeded for %s — no state advance (poll job dispatched)",
                        job_type.value, server_id,
                    )
                await message.ack()

            else:  # FAILED
                logger.error(
                    "[mgn] %s failed for %s: %s — sending to DLX",
                    job_type.value, server_id, result.error,
                )
                await message.nack(requeue=False)

        except Exception as e:
            logger.error("[mgn] Unexpected error processing message: %s", e, exc_info=True)
            await message.nack(requeue=False)

    async def _dispatch(self, job_type: JobType, payload: dict) -> None:
        """Publish a follow-up job (typically a poll job) to its queue."""
        queue_name = QUEUE_ROUTING[job_type] #dispatcher maps all the different jobs to the job type
        body = {
            "job_type": job_type.value,
            "payload":  payload,
            "attempt":  1,
        }
        await self._channel.default_exchange.publish(
            Message(
                body=json.dumps(body).encode(),
                delivery_mode=DeliveryMode.PERSISTENT,
                content_type="application/json",
            ),
            routing_key=queue_name,
        ) #default exchange: routing key matches the queue name
        logger.info("[mgn] Dispatched follow-up %s for server %s", job_type.value, payload.get("server_id"))

    # ------------------------------------------------------------------
    # Handlers
    #
    # Each handler receives the job payload and returns an MgnResult.
    # AWS API calls live here. Handlers know nothing about RabbitMQ or
    # ack/nack — that logic belongs entirely in _on_message above.
    # ------------------------------------------------------------------

    # ── Stage 1: Onboarding ──────────────────────────────────────────

    async def _handle_add_server(self, payload: dict) -> MgnResult:
        """
        Verifies the source server is registered in MGN with the agent installed.
        The agent is installed externally on the source machine; this handler
        confirms MGN has detected it before advancing state.

        AWS call: mgn.describe_source_servers
        Complete when: server appears in MGN and lifecycle state is not DISCONNECTED
        """
        server_id           = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            response = self._mgn.describe_source_servers(
                filters={"sourceServerIDs": [aws_source_server_id]}
            )
            items = response.get("items", [])

            if not items:
                return MgnResult.failed(
                    f"Server {aws_source_server_id} not found in MGN — agent may not be installed yet"
                )

            server     = items[0]
            life_state = server.get("lifeCycle", {}).get("state", "")

            if life_state == "DISCONNECTED":
                return MgnResult.failed(
                    f"Server {aws_source_server_id} is DISCONNECTED in MGN — agent is not active"
                )

            logger.info(
                "[mgn] Server %s confirmed in MGN (aws_id=%s, lifecycle=%s)",
                server_id, aws_source_server_id, life_state,
            )

            # Store the aws_source_server_id in the servers table so other
            # handlers and the Rollback Worker can reference it without
            # requiring it to be in every message payload.
            await self._state_manager.set_aws_source_server_id(server_id, aws_source_server_id)

            return MgnResult.success(
                next_state="AGENT_INSTALLED",
                metadata={"aws_source_server_id": aws_source_server_id, "lifecycle_state": life_state},
            )

        except Exception as e:
            return MgnResult.failed(f"MGN describe_source_servers error: {e}")

    # ── Stage 2: Replication Setup ───────────────────────────────────

    async def _handle_configure_replication(self, payload: dict) -> MgnResult:
        """
        Configures the MGN replication settings for the source server.
        Sets the staging area subnet, replication server instance type,
        security groups, and data plane routing.

        After calling the MGN API, advances through two states:
          AGENT_INSTALLED → REPLICATION_CONFIGURED → AWAITING_REPLICATION_APPROVAL
        The second advance is done directly here before returning so that
        the approval gate is set atomically with the configuration step.

        AWS call: mgn.update_replication_configuration
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            self._mgn.update_replication_configuration(
                sourceServerID=aws_source_server_id,
                stagingAreaSubnetId=payload["staging_subnet_id"],
                replicationServerInstanceType=payload.get("replication_instance_type", "t3.small"),
                replicationServersSecurityGroupsIDs=payload["replication_security_group_ids"],
                dataPlaneRouting=payload.get("data_plane_routing", "PRIVATE_IP"),
                defaultLargeStagingDiskType=payload.get("staging_disk_type", "GP2"),
            )

            logger.info("[mgn] Replication configured for %s", server_id)

            # First advance: configuration is done
            await self._state_manager.advance_state(
                server_id,
                "REPLICATION_CONFIGURED",
                job_type="configure_replication_settings",
                metadata={"aws_source_server_id": aws_source_server_id},
            )

            # Second advance: move to the human approval gate immediately.
            # The system pauses here until an engineer approves the security
            # group configuration before replication actually starts.
            return MgnResult.success(
                next_state="AWAITING_REPLICATION_APPROVAL",
                metadata={"aws_source_server_id": aws_source_server_id},
            )

        except Exception as e:
            return MgnResult.failed(f"MGN update_replication_configuration error: {e}")

    async def _handle_start_replication(self, payload: dict) -> MgnResult:
        """
        Starts MGN replication for the source server. Called after the
        replication approval gate is resolved by an engineer.
        State is already REPLICATION_STARTED (set by the approval gate).

        After starting, dispatches POLL_REPLICATION_STATUS which will
        advance the server to READY_FOR_TESTING once replication lag
        reaches zero and MGN marks the server as ready for test.

        AWS call: mgn.start_replication
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            self._mgn.start_replication(sourceServerID=aws_source_server_id)
            logger.info("[mgn] Replication started for %s", server_id)

            await self._dispatch(
                JobType.POLL_REPLICATION_STATUS,
                {"server_id": server_id, "aws_source_server_id": aws_source_server_id},
            )

            # next_state=None: state was already set to REPLICATION_STARTED
            # by the approval gate. The poll job will advance it to READY_FOR_TESTING.
            return MgnResult.success(next_state=None)

        except Exception as e:
            return MgnResult.failed(f"MGN start_replication error: {e}")

    # ── Stage 3: Test Launch ─────────────────────────────────────────

    async def _handle_configure_test_launch_template(self, payload: dict) -> MgnResult:
        """
        Configures the MGN launch template for the test instance.
        Sets launch disposition to TEST, instance type, licensing, etc.

        Advances through two states:
          READY_FOR_TESTING → TEST_LAUNCH_TEMPLATE_CONFIGURED → AWAITING_TEST_LAUNCH_APPROVAL

        AWS call: mgn.update_launch_configuration
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            self._mgn.update_launch_configuration(
                sourceServerID=aws_source_server_id,
                launchDisposition="TEST",
                targetInstanceTypeRightSizingMethod=payload.get("right_sizing", "NONE"),
                licensing={"osByol": payload.get("byol", False)},
            )

            logger.info("[mgn] Test launch template configured for %s", server_id)

            await self._state_manager.advance_state(
                server_id,
                "TEST_LAUNCH_TEMPLATE_CONFIGURED",
                job_type="configure_test_launch_template",
                metadata={"aws_source_server_id": aws_source_server_id},
            )

            return MgnResult.success(
                next_state="AWAITING_TEST_LAUNCH_APPROVAL",
                metadata={"aws_source_server_id": aws_source_server_id},
            )

        except Exception as e:
            return MgnResult.failed(f"MGN update_launch_configuration (test) error: {e}")

    async def _handle_launch_test_instance(self, payload: dict) -> MgnResult:
        """
        Launches the MGN test instance. Called after the test launch
        approval gate is resolved. State is already TEST_INSTANCE_LAUNCHING.

        MGN returns a Job object. The actual EC2 instance ID is not
        immediately available — it appears once the job completes.
        POLL_TEST_INSTANCE_STATUS monitors the job until the instance
        is running and extracts the instance ID from the job result.

        AWS call: mgn.start_test
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            response = self._mgn.start_test(sourceServerIDs=[aws_source_server_id])
            job_id   = response.get("job", {}).get("jobID")

            logger.info("[mgn] Test instance launch started for %s (mgn_job_id=%s)", server_id, job_id)

            await self._dispatch(
                JobType.POLL_TEST_INSTANCE_STATUS,
                {
                    "server_id":            server_id,
                    "aws_source_server_id": aws_source_server_id,
                    "mgn_job_id":           job_id,
                },
            )

            return MgnResult.success(next_state=None, metadata={"mgn_job_id": job_id})

        except Exception as e:
            return MgnResult.failed(f"MGN start_test error: {e}")

    async def _handle_finalize_test(self, payload: dict) -> MgnResult:
        """
        Cleans up after test validation by terminating the test instance
        through MGN. Using MGN's TerminateTargetInstances keeps all AWS
        calls in one interface — no EC2 client needed.

        Advances: TEST_FINALIZED → CUTOVER_LAUNCH_TEMPLATE_CONFIGURED
        The cutover launch template will be configured in the next job.

        AWS call: mgn.terminate_target_instances
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            self._mgn.terminate_target_instances(sourceServerIDs=[aws_source_server_id])
            logger.info("[mgn] Test instance termination requested for %s", server_id)

            return MgnResult.success(
                next_state="CUTOVER_LAUNCH_TEMPLATE_CONFIGURED",
                metadata={"aws_source_server_id": aws_source_server_id},
            )

        except Exception as e:
            return MgnResult.failed(f"MGN terminate_target_instances (test) error: {e}")

    # ── Stage 4: Cutover ─────────────────────────────────────────────

    async def _handle_configure_cutover_launch_template(self, payload: dict) -> MgnResult:
        """
        Configures the MGN launch template for the cutover (production) instance.
        Production configuration typically differs from test — different instance
        type, IAM role, network placement, and security groups.

        Advances: CUTOVER_LAUNCH_TEMPLATE_CONFIGURED → AWAITING_CUTOVER_LAUNCH_APPROVAL

        AWS call: mgn.update_launch_configuration
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            self._mgn.update_launch_configuration(
                sourceServerID=aws_source_server_id,
                launchDisposition="CUTOVER",
                targetInstanceTypeRightSizingMethod=payload.get("right_sizing", "NONE"),
                licensing={"osByol": payload.get("byol", False)},
            )

            logger.info("[mgn] Cutover launch template configured for %s", server_id)

            return MgnResult.success(
                next_state="AWAITING_CUTOVER_LAUNCH_APPROVAL",
                metadata={"aws_source_server_id": aws_source_server_id},
            )

        except Exception as e:
            return MgnResult.failed(f"MGN update_launch_configuration (cutover) error: {e}")

    async def _handle_start_cutover(self, payload: dict) -> MgnResult:
        """
        Starts the MGN cutover. MGN performs a final replication delta sync
        to minimise data loss, then launches the cutover instance.
        State is already CUTOVER_STARTED (set by the approval gate).

        POLL_CUTOVER_SYNC_STATUS monitors until the sync completes and MGN
        is ready to launch the instance. That poll advances to READY_FOR_CUTOVER_LAUNCH.

        AWS call: mgn.start_cutover
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            response = self._mgn.start_cutover(sourceServerIDs=[aws_source_server_id])
            job_id   = response.get("job", {}).get("jobID")

            logger.info("[mgn] Cutover started for %s (mgn_job_id=%s)", server_id, job_id)

            # Advance immediately so the poll arrives at the expected state.
            # VALID_STATES_FOR_POLL maps POLL_CUTOVER_SYNC_STATUS → CUTOVER_SYNC_IN_PROGRESS.
            await self._state_manager.advance_state(
                server_id,
                "CUTOVER_SYNC_IN_PROGRESS",
                job_type="start_cutover",
                metadata={"aws_source_server_id": aws_source_server_id},
            )

            await self._dispatch(
                JobType.POLL_CUTOVER_SYNC_STATUS,
                {
                    "server_id":            server_id,
                    "aws_source_server_id": aws_source_server_id,
                    "mgn_job_id":           job_id,
                },
            )

            return MgnResult.success(next_state=None, metadata={"mgn_job_id": job_id})

        except Exception as e:
            return MgnResult.failed(f"MGN start_cutover error: {e}")

    async def _handle_launch_cutover_instance(self, payload: dict) -> MgnResult:
        """
        Confirms the cutover EC2 instance has been launched by MGN as part
        of start_cutover. Retrieves the instance ID from MGN and dispatches
        POLL_CUTOVER_INSTANCE_STATUS to monitor until it reaches 'running'.

        MGN launches the instance automatically after the final sync — this
        handler does not make a launch API call, it extracts the instance ID
        so the poll job can monitor it.

        AWS call: mgn.describe_source_servers (to get launched instance ID)
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            response  = self._mgn.describe_source_servers(
                filters={"sourceServerIDs": [aws_source_server_id]}
            )
            items     = response.get("items", [])

            if not items:
                return MgnResult.failed(f"Server {aws_source_server_id} not found in MGN")

            cutover_job = items[0].get("lifeCycle", {}).get("lastCutover", {})
            instance_id = cutover_job.get("initiated", {}).get("jobID")

            # The actual EC2 instance ID lives in the job details — extract it
            # so POLL_CUTOVER_INSTANCE_STATUS can call ec2.describe_instances directly.
            job_response = self._mgn.describe_jobs(filters={"jobIDs": [instance_id]}) if instance_id else {}
            ec2_instance_id = None
            for job in job_response.get("items", []):
                for participant in job.get("participatingServers", []):
                    if participant.get("sourceServerID") == aws_source_server_id:
                        ec2_instance_id = participant.get("launchedEc2InstanceID")
                        break

            logger.info(
                "[mgn] Cutover instance confirmed for %s (ec2_id=%s)",
                server_id, ec2_instance_id,
            )

            # Advance immediately so the poll arrives at the expected state.
            # VALID_STATES_FOR_POLL maps POLL_CUTOVER_INSTANCE_STATUS → CUTOVER_INSTANCE_LAUNCHING.
            await self._state_manager.advance_state(
                server_id,
                "CUTOVER_INSTANCE_LAUNCHING",
                job_type="launch_cutover_instance",
                metadata={"aws_source_server_id": aws_source_server_id, "cutover_instance_id": ec2_instance_id},
            )

            await self._dispatch(
                JobType.POLL_CUTOVER_INSTANCE_STATUS,
                {
                    "server_id":            server_id,
                    "aws_source_server_id": aws_source_server_id,
                },
            )

            return MgnResult.success(
                next_state=None,
                metadata={"cutover_instance_id": ec2_instance_id},
            )

        except Exception as e:
            return MgnResult.failed(f"MGN describe_source_servers (cutover instance) error: {e}")

    async def _handle_finalize_cutover(self, payload: dict) -> MgnResult:
        """
        Marks the migration as complete in MGN. Called after the engineer
        validates that the production cutover instance is working correctly.

        Advances: CUTOVER_FINALIZED → DISCONNECTING
        Then dispatches DISCONNECT_SOURCE_SERVER to begin cleanup.

        AWS call: mgn.finalize_cutover
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            self._mgn.finalize_cutover(sourceServerIDs=[aws_source_server_id])
            logger.info("[mgn] Cutover finalised for %s", server_id)

            # Advance state first, then dispatch the next job.
            # The dispatch is done here rather than returning next_state=DISCONNECTING
            # because we also need to enqueue the DISCONNECT job in the same step.
            await self._state_manager.advance_state(
                server_id,
                "DISCONNECTING",
                job_type="finalize_cutover",
                metadata={"aws_source_server_id": aws_source_server_id},
            )

            await self._dispatch(
                JobType.DISCONNECT_SOURCE_SERVER,
                {"server_id": server_id, "aws_source_server_id": aws_source_server_id},
            )

            # next_state=None because we already called advance_state directly above.
            return MgnResult.success(next_state=None)

        except Exception as e:
            return MgnResult.failed(f"MGN finalize_cutover error: {e}")

    # ── Stage 5: Post-Migration Cleanup ──────────────────────────────

    async def _handle_disconnect_source_server(self, payload: dict) -> MgnResult:
        """
        Disconnects the source server from MGN replication. Called
        automatically after finalize_cutover. State is DISCONNECTING.

        POLL_DISCONNECT_STATUS monitors until MGN confirms the server
        is fully disconnected, then advances state to DISCONNECTED.

        AWS call: mgn.disconnect_from_service
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            self._mgn.disconnect_from_service(sourceServerID=aws_source_server_id)
            logger.info("[mgn] Disconnect initiated for %s", server_id)

            await self._dispatch(
                JobType.POLL_DISCONNECT_STATUS,
                {"server_id": server_id, "aws_source_server_id": aws_source_server_id},
            )

            return MgnResult.success(next_state=None)

        except Exception as e:
            return MgnResult.failed(f"MGN disconnect_from_service error: {e}")

    async def _handle_archive_source_server(self, payload: dict) -> MgnResult:
        """
        Archives the source server in MGN. Called after the engineer confirms
        it is safe to remove it from active MGN migrations (approval gate resolved).
        State is ARCHIVED when this job arrives.

        Advances: ARCHIVED → AWAITING_CLEANUP_APPROVAL
        The cleanup gate lets an engineer confirm it is safe to delete the
        replication infrastructure before the final AWS resources are removed.

        AWS call: mgn.mark_as_archived
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            self._mgn.mark_as_archived(sourceServerIDs=[aws_source_server_id])
            logger.info("[mgn] Server %s archived in MGN", server_id)

            return MgnResult.success(
                next_state="AWAITING_CLEANUP_APPROVAL",
                metadata={"aws_source_server_id": aws_source_server_id},
            )

        except Exception as e:
            return MgnResult.failed(f"MGN mark_as_archived error: {e}")

    async def _handle_delete_replication_components(self, payload: dict) -> MgnResult:
        """
        Deletes the source server record and all associated replication
        infrastructure from MGN. This is the final cleanup step — it removes
        the replication server, staging area, and all MGN metadata.

        State is CLEANUP_COMPLETE when this job arrives — no further state
        advance is made. This is purely a cleanup AWS call.

        AWS call: mgn.delete_source_server
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            self._mgn.delete_source_server(sourceServerID=aws_source_server_id)
            logger.info(
                "[mgn] Replication components deleted for server %s (aws_id=%s)",
                server_id, aws_source_server_id,
            )

            # next_state=None — CLEANUP_COMPLETE is already the final state.
            return MgnResult.success(next_state=None)

        except Exception as e:
            return MgnResult.failed(f"MGN delete_source_server error: {e}")
