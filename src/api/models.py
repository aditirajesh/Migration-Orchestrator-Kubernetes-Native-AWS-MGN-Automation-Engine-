from datetime import datetime

from pydantic import BaseModel


# -----------------------------------------------------------------------
# Request models — per-server
# -----------------------------------------------------------------------

class RegisterServerRequest(BaseModel):
    server_id:          str
    hostname:           str
    ip_address:         str
    aws_account_id:     str | None = None
    aws_region:         str | None = None
    assigned_engineer:  str | None = None
    batch_id:           str | None = None


class StartServerRequest(BaseModel):
    # The MGN-assigned source server ID. The MGN agent must already be
    # installed on the source machine before calling /start.
    aws_source_server_id: str


class ConfigureReplicationRequest(BaseModel):
    staging_subnet_id:               str
    replication_security_group_ids:  list[str]
    replication_instance_type:       str  = "t3.small"
    data_plane_routing:              str  = "PRIVATE_IP"
    staging_disk_type:               str  = "GP2"


class ConfigureTestLaunchRequest(BaseModel):
    right_sizing:  str  = "NONE"
    byol:          bool = False


class ConfigureCutoverLaunchRequest(BaseModel):
    right_sizing:  str  = "NONE"
    byol:          bool = False


class ApproveRequest(BaseModel):
    engineer_id:  str
    notes:        str | None = None


class RejectRequest(BaseModel):
    engineer_id:  str
    reason:       str


class ResetRequest(BaseModel):
    engineer_id:  str
    reason:       str | None = None


# -----------------------------------------------------------------------
# Request models — bulk per-server operations
# -----------------------------------------------------------------------

class BulkRegisterItem(BaseModel):
    server_id:          str
    hostname:           str
    ip_address:         str
    aws_account_id:     str | None = None
    aws_region:         str | None = None
    assigned_engineer:  str | None = None
    batch_id:           str | None = None


class BulkRegisterRequest(BaseModel):
    servers: list[BulkRegisterItem]


class BulkStartItem(BaseModel):
    server_id:            str
    aws_source_server_id: str


class BulkStartRequest(BaseModel):
    servers: list[BulkStartItem]


# -----------------------------------------------------------------------
# Request models — batch management
# -----------------------------------------------------------------------

class CreateBatchRequest(BaseModel):
    batch_id:     str
    name:         str
    description:  str | None = None
    created_by:   str


class AddServersToBatchRequest(BaseModel):
    server_ids: list[str]


# -----------------------------------------------------------------------
# Request models — batch pipeline actions
# -----------------------------------------------------------------------

class BatchConfigureReplicationRequest(BaseModel):
    staging_subnet_id:               str
    replication_security_group_ids:  list[str]
    replication_instance_type:       str  = "t3.small"
    data_plane_routing:              str  = "PRIVATE_IP"
    staging_disk_type:               str  = "GP2"


class BatchConfigureTestLaunchRequest(BaseModel):
    right_sizing:  str  = "NONE"
    byol:          bool = False


class BatchConfigureCutoverLaunchRequest(BaseModel):
    right_sizing:  str  = "NONE"
    byol:          bool = False


class BatchApproveRequest(BaseModel):
    engineer_id:  str
    notes:        str | None = None


class BatchRejectRequest(BaseModel):
    engineer_id:  str
    reason:       str


# -----------------------------------------------------------------------
# Response models — per-server
# -----------------------------------------------------------------------

class ServerResponse(BaseModel):
    server_id:             str
    hostname:              str
    ip_address:            str
    aws_source_server_id:  str | None
    aws_account_id:        str | None
    aws_region:            str | None
    current_state:         str
    previous_state:        str | None
    assigned_engineer:     str | None
    batch_id:              str | None
    created_at:            datetime
    updated_at:            datetime


class TransitionResponse(BaseModel):
    transition_id:  str
    server_id:      str
    from_state:     str
    to_state:       str
    job_id:         str | None
    job_type:       str | None
    triggered_by:   str
    timestamp:      datetime
    metadata:       dict | None


class ActionResponse(BaseModel):
    message:    str
    server_id:  str


# -----------------------------------------------------------------------
# Response models — bulk operations
# -----------------------------------------------------------------------

class BulkItemResult(BaseModel):
    server_id:  str
    status:     str        # "succeeded" | "failed"
    detail:     str | None = None


class BulkActionResponse(BaseModel):
    succeeded:  list[BulkItemResult]
    failed:     list[BulkItemResult]


# -----------------------------------------------------------------------
# Response models — batch management
# -----------------------------------------------------------------------

class BatchResponse(BaseModel):
    batch_id:               str
    name:                   str
    description:            str | None
    created_by:             str
    created_at:             datetime
    replication_config:     dict | None
    test_launch_config:     dict | None
    cutover_launch_config:  dict | None
    server_count:           int


class AddServersToBatchResponse(BaseModel):
    batch_id:          str
    assigned:          list[str]
    already_assigned:  list[str]
    not_found:         list[str]


# -----------------------------------------------------------------------
# Response models — batch pipeline actions
# -----------------------------------------------------------------------

class BatchActionResponse(BaseModel):
    batch_id:   str
    succeeded:  list[str]
    skipped:    list[str]
    failed:     list[str]
