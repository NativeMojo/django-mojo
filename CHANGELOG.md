
## v0.1.3 - May 29, 2025
## v0.1.38 - September 24, 2025

* Added ticket category


## v0.1.37 - September 23, 2025

* FIX job reaper falsely kill done jobs


## v0.1.36 - September 23, 2025

* fixing filtering on no related models


## v0.1.35 - September 23, 2025

* FIX cron scheduling


## v0.1.34 - September 22, 2025

* Fixed advanced filtering


## v0.1.33 - September 22, 2025

* Ticket bug fix


## v0.1.32 - September 21, 2025

* Added new auto security checks on rest end points


## v0.1.31 - September 18, 2025

* Last fix did not take


## v0.1.30 - September 18, 2025

* ANother bug fix in jobs claiming jobs it cannot run


## v0.1.29 - September 18, 2025

* BUGFIX infinite retries on import func errors


## v0.1.28 - September 18, 2025

* BUGFIX job select_for_update bug


## v0.1.27 - September 18, 2025

* Debugging for jobs engine


## v0.1.26 - September 17, 2025

* Minor fixes in metrics and activity tracking


## v0.1.25 - September 16, 2025

* Added: more helpers to testit
* Added: more logic for redis pool and "with syntax"


## v0.1.24 - September 12, 2025

* New status commands


## v0.1.23 - September 12, 2025

* BUGFIX saving metrics perms


## v0.1.22 - September 10, 2025

* FIX for serverless/clusters


## v0.1.21 - September 10, 2025

* More servless bug fixes


## v0.1.20 - September 10, 2025

* BUG fixing serverless valkey/redis


## v0.1.19 - September 09, 2025

* attempting to fix pipeline bugs


## v0.1.18 - September 09, 2025

fixing redis auth


## v0.1.17 - September 09, 2025

* Fix pyright auto importing wrong modules


## v0.1.16 - September 09, 2025



## v0.1.15 - September 09, 2025

  * Major cleanup and new features see docs


## v0.1.14 - July 08, 2025

  CLEANUP and UnitTests for tasks


## v0.1.13 - June 09, 2025

   ADDED fileman app, a complete filemanager for django with rendition support and multiple backends and renderers
   UPDATED simple serializer greatly improved and new advanced serializer with support for other output formats
   UPDATED incidents subsystem for handling system events, rules and incidents
   


## v0.1.10 - June 06, 2025

   CHANGED license from MIT to Apache 2.0
   ADDED to new fileman app with file storage
   ADDED new notify framework that support mail, sms, etc
   ADDED crypto support for hmac signing and verifying
   ADDED more tests
   NOTE framework is not ready for primetime yet, but soon


## v0.1.9 - June 04, 2025

   UPDATE moved mojo tests into mojo project root, but still require a django project to run
   FIXED crypto encrypt,decrypt, and hash with proper tests
   ADDED incident system for report events and having them trigger incidents, including rules engine
   ADDED MojoSecrets which allows storing of secret encrypted data into a model
   ADDED helper scripts for talking to godaddy api and automating SES setup
   ADDED new mail handling system (work in progress)


## v0.1.8 - June 01, 2025

  Updaing version info and tagging release


## v0.1.7 - June 01, 2025

   Updating version info and release


## v0.1.4 - May 30, 2025

  ADDED: lots of improvements to making metrics cleaner and passing all tests
  ADDED: mojo JsonResponse to use ujson and ability to add future logic for custom handling of certain data


## v0.1.3 - May 29, 2025

  ADDED support to ignore github release and use tags


## v0.1.3 - May 29, 2025

  ADDED: more robust publishing, including github releases



  CLEANUP: moved django apps into apps folder to be more readable
  ADDED: more utility functions and trying to use more builting functions and less custom
  ADDED: useragent parsing and remote ip
  ADDED: support for nested apps
  ADDED: version info to default api
  ADDED: testit support for django_unit_setup and django_unit_test in django env