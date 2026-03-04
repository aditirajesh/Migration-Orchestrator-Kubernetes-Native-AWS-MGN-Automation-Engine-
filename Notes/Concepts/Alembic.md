#### It forms a linked list of migrations.

## Core Mechanism:

for each migration file it creates it has three critical parts:
- revision ID
- The previous revision ID (called down_revision)
- upgrade() function - SQL to apply the change 
- downgrade() function - SQL to reverse the change. 

This is an example of the linked list of migration changes that gets formed:
```
  None ← 001_create_servers ← 002_create_history ← 003_add_aws_region ← HEAD
```

  When you run alembic upgrade head, Alembic walks this chain from the last applied migration to HEAD, applying each upgrade() in order. When you run alembic downgrade -1, it runs the downgrade() of the
   most recently applied migration and steps one link back.
  
  **Note: alembic migration deals solely with the structure of the state manager tables, it does not deal with any of the actual migration of the source tables.**

## The Alembic version table 
Alembic creates one table in your database that has only one column (called version_num). This version_num has only one row value which points to the revision ID, indicating this is the revision ID of the last successfully applied migration 

When an upgrade command is read, this version table is read and if the upgrade is successful, this version table is updated with the new revision id. Otherwise, it remains unchanged which ensures the database stays consistent 

## Configuration Files 
- alembic.ini - contains all the parameters, logging settings, where migration lives, etc
- env.py - configuring the migration environment 
- script.py.mako - rough skeleton on how the migration files are supposed to look like 
- versions/ - individual migration files live here. 

## How does alembic work with postgres image?
Postgres image is referenced in the statefulset -> to create a database in kubernetes. The actual data is present in the running container which has a network connection, and this data either lives in the container runtime or in the PVC attached to the statefulset. In this case, nothing is baked into the image. 

![[Pasted image 20260225140655.png]]

## How do we see the actual table of servers then
Couple of alternatives exist:
1. Directly exec into the container, run postgres and select tables to see
2. run a pgadmin container with a port exposed (for example, port 50). Then run localhost:50 to get a full visual table browser. 
3. Use TablePlus or DBeaver. 