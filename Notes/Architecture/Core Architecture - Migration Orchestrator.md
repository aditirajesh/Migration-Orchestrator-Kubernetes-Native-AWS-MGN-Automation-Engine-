
### It is a distributed state machine where each server that needs to be migrated moves through well-defined states, with jobs executed by workers and their states tracked in a central database. 

![[img1.png]]                                                   Architecture with AWS Services

### The Core Problem:
- The main problem with current migrations is that it is a multi-step, multi-hour process involving multiple external APIs, human interventions. 
- Steps take hours -> if steps fail then all previous steps executed should be known by the user. 
- An engineer can't work on multiple source servers simultaneously. 
- Prone to errors especially with launch template and replication template configuration which is very tedious to do manually on the console. 
- There is no proper trail of what has been done on manual migrations
- Not possible to migrate multiple servers in parallel. 

This architecture aims to solve all these problems. 

## Components:

### Orchestrator API:
- It is the only external interface for any human/ system inputs. All human and system triggers go through here. 
- Nothing writes directly to the database except the orchestrator API, which acts as a gate. 
- This is one authoritative place for all reads and writes to happen, to prevent concurrent access issues and re-writing of states. This makes sure that states are transitive in nature, and are not just randomly altered by the workers. 
- This is also used to validate the states. 
- **Why FastAPI:** Because this is async-native. This will handle multiple requests (from Redis, AWS APIs, DB, human triggers) in an asynchronous manner which will save a lot of time. It also has built-in websockets to send updates in real-time. 
- In comparison, Flask can also be used but it is not async-native, and it would be harder to scale for IO-heavy workloads since it would mostly rely on threading for job execution. 

### State Manager:
- Python module that owns all logic about which state transitions are legal and has a record of all the transitions 
- Without this, the APIs, servers, would all have separate state records which would be harder to trace and in the event of an error where a state is skipped, the bug would be impossible to trace. 
- There should be locking: suppose two workers are working on the same server and are trying to change the state of the server, only one should be placed into effect. Locks in POSTGRESQL can serve this purpose. 

### Job Dispatcher:
- A module that is used to obtain job type + attributes and accordingly dispatch the job into an SQS queue. 
- Without this module, every piece of code that needs to enqueue a job to work on would need to be aware of what queue it needs to enqueue, how to format the message, etc all of which is taken care of by the job dispatcher. 
- FIFO Queues are used for this purpose to ensure that the state orders are respected. 

### Approval Service:
- A python module that has manual gates for human/external API input. When something reaches the gate, it makes a record of and sends a notification. 
- Some parts of migration such as security group rules, approving launch templates, launching final cutover needs to be manually approved by human intervention. This is then in-turn used to dispatch the next set of jobs. 

### MGN Worker:
- Python module that is used to execute jobs present in the mgn-job FIFO queue. It polls the queue, gets a job and executes it. 
- API cannot execute long-running jobs asynchronously. The MGN worker helps with this since it starts the job and runs it in the background, and the API can move forward to execute the next request. 

### Poller Worker:
- This pulls jobs from the poll_jobs SQS queue and executes the job. This handles the long-running status checks of the external AWS APIs. It polls the API in periodic time intervals to see if the status of a certain job has changed and accordingly takes an action. Then it reports back to the orchestrator API and updates the state manager 

### Rollback Worker:
- Pulls from rollback_jobs queue. In the event of a failed job, It looks through the state transition table to properly reverse the process step by step, and then reports the completion back to the API so that it can mark the server to be in a safe state. 


## Implementation:
![[img2.png]]



![[img3.png]]                                     Architecture w/ Portable alternative



