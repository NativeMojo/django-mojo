
## v0.1.3 - May 29, 2025
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

