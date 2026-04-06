Running TODO List

 - Look into a flag SERIALIZATION_USE_ISO_8601 that converts on datetime serialization into ISO 8601
 - Look into pruning incidents after 90 days when resolved, (ossec after 7 days?)
 
  - Security Dashboard
  - Nightly overview from LLM of System Status and thread analysis
  -
  
  AI Assistant
   - new block types
   - planning?
   - show how long a tool runs for

   Lets think about how we handle user enterring multiple enters before job is done
  
  Ability to Browse S3 Buckets
  We want simple ability for the UI to provide a simple UI to browse the S3 buckets folder structure (FileManager) if the user has access to it?
  
  CronJobs User Scheduling
    - we want users or Assistant the ability to schedule cron jobs to run in the future
    - we want a Crud Interface to manage these jobs
    - An example of a job could be an LLM task where it is a specific set of extructions for an LLM to execute
        - "send me an email every sunday with a list of merchants who haven;t transacted in last 4 days"
        - "check once an hour for any merchant that has more then 4 transactions and 50% declined rate"
