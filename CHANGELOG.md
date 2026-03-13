
## v0.1.3 - May 29, 2025
## v1.0.39 - March 12, 2026

new notification system made easy


## v1.0.38 - March 12, 2026

bug fix in refresh token not have correct expiry


## v1.0.37 - March 12, 2026

fixing bug in sms login, fixing bug in tests


## v1.0.36 - March 12, 2026

typo fix


## v1.0.35 - March 12, 2026

support username in sms login


## v1.0.34 - March 12, 2026

bug fixes, more security patches


## v1.0.33 - March 12, 2026

improve MFA support


## v1.0.32 - March 11, 2026

ability to login with phonenumber


## v1.0.31 - March 11, 2026

new rate limiting login


## v1.0.30 - March 11, 2026

proper phone hub endpoints


## v1.0.29 - March 11, 2026

making some common phone apis publlic


## v1.0.28 - March 08, 2026

don't save when only doing model actions


## v1.0.27 - March 08, 2026

fixing api key permission checks
fixing false test


## v1.0.26 - March 08, 2026

bugfix for metrics decorators


## v1.0.25 - March 07, 2026

streamlined response with simile dicts now


## v1.0.24 - March 04, 2026

NEW django cache support to deal with collisions using django-redis-cache


## v1.0.23 - March 03, 2026

save api keys
- Added first-party Django Redis cache backend: `mojo.cache.MojoRedisCache` (replaces `redis_cache.RedisCache` usage).
- Added migration docs for cache backend settings and dependency cleanup.


## v1.0.22 - March 03, 2026

New feature to send and wait for events to come back


## v1.0.21 - March 01, 2026

new content guard


## v1.0.20 - February 27, 2026

* superuser rightfully has all permissions


## v1.0.19 - February 26, 2026

new oauth flows


## v1.0.18 - February 24, 2026

- New API KEYs support, new rate limit decorators, and metrics decorators


## v1.0.17 - February 12, 2026

BUGFIX for OneToOne fields


## v1.0.16 - February 12, 2026

NEW FILEVAULT APP


## v1.0.15 - February 10, 2026

* ADDED auto email templates
* Cleanup of filemaner and is_public check


## v1.0.14 - February 07, 2026

* Major cleanup of domain utils


## v1.0.13 - February 01, 2026

* BUGFIX USPS requires caps on states


## v1.0.12 - February 01, 2026

* Fixing Phone lookup for international numbers


## v1.0.11 - February 01, 2026

* Improved API key access
* better docs for realtime and metricsw
* new improved ability to have absolute routing ie prefix with /
* major bug fix in cron parsing of multiple times
* new domain helper utility


## v1.0.10 - December 24, 2025

bug fix for issue when multiple people access IoT lock


## v1.0.9 - December 17, 2025

bug fix when using list helpers
allow incidents to ignore rules


## v1.0.8 - December 11, 2025

fix for iso format null


## v1.0.7 - December 09, 2025

fixing rule field to text


## v1.0.6 - December 06, 2025

fixing bug when lock syncs via realtime more then once


## v1.0.5 - December 06, 2025

fixing realtime debugging


## v1.0.4 - December 04, 2025

bug fix when using isnull=False


## v1.0.3 - December 03, 2025

bug fix in sync of metadata


## v1.0.2 - December 03, 2025

fixing bug in fetching category data


## v1.0.1 - December 03, 2025

we are ready for 1.0 release


## v0.1.141 - December 03, 2025

fixing bug in category


## v0.1.140 - December 03, 2025

* adding scope to security events


## v0.1.139 - December 03, 2025

fixing bug in calculating totals


## v0.1.138 - December 01, 2025

missing fileman migrations


## v0.1.137 - December 01, 2025

* improvements to file handling
* improvements to metrics labeling weekly


## v0.1.136 - November 25, 2025

BUGFIX search


## v0.1.135 - November 25, 2025

BUGFIX: membership not propogating


## v0.1.134 - November 23, 2025

* missing migration file


## v0.1.133 - November 23, 2025

BUGFIX in bundling incidents by rules


## v0.1.132 - November 21, 2025

BUGFIX is broadcast messages


## v0.1.131 - November 21, 2025

publish broadcast async


## v0.1.130 - November 21, 2025

Adding server name to incidents so cyber engine can do action on one server


## v0.1.129 - November 19, 2025

syntax error


## v0.1.128 - November 19, 2025

BUGFIX in permissions for member invites


## v0.1.127 - November 19, 2025

* BUGFIX tier level access for a platform vs kyc customer


## v0.1.126 - November 19, 2025

BUGFIX when publishing templates with non native types


## v0.1.125 - November 19, 2025

fixing issue when inviting kyc client vs customer


## v0.1.124 - November 18, 2025

HOTFIX cyber report downloads failing in csv format


## v0.1.123 - November 18, 2025

* ADDED logic for improved date handling in relation to government ids"


## v0.1.122 - November 17, 2025

* CRITICAL FIX in log permissions fail gracefully
* sysinfo in correct fields
* improved email template handling


## v0.1.120 - November 01, 2025

No auth required for address suggestions


## v0.1.119 - October 31, 2025

Updating geo location


## v0.1.118 - October 30, 2025

Another TYPO


## v0.1.117 - October 30, 2025

TYPO in fcm (push notifications)


## v0.1.116 - October 30, 2025

ability to log Push notifications for debugging


## v0.1.115 - October 28, 2025

New Phonehub, qrcode, improved testit


## v0.1.114 - October 26, 2025

Advanced Compliance features


## v0.1.113 - October 24, 2025

NEW phonehub which provide detailed compliance for phone numbers


## v0.1.112 - October 22, 2025

BUGFIX searching for group members


## v0.1.111 - October 22, 2025

bugfix allow user to subscribe to self


## v0.1.110 - October 21, 2025

more socket cleanup


## v0.1.109 - October 21, 2025

Custom FCM implementation to work around issues


## v0.1.108 - October 21, 2025

Cleanup of FCM


## v0.1.107 - October 21, 2025

* BUGFIXES in rules and events


## v0.1.105 - October 17, 2025

* New incident engine cleanup


## v0.1.104 - October 16, 2025

Missing key migrations


## v0.1.103 - October 16, 2025

HOTFIX raw json lists in posts not handled correctly


## v0.1.102 - October 15, 2025

Update geo ip for forensics


## v0.1.101 - October 15, 2025

Config to allow incident and rule deletion


## v0.1.100 - October 15, 2025

* Cleanup and debugging of rules and incidents


## v0.1.99 - October 15, 2025

HOTFIX - shared context bug with requests


## v0.1.98 - October 14, 2025

Invite tokens


## v0.1.97 - October 13, 2025

HOTFIX don't show protected fields in changes


## v0.1.96 - October 13, 2025

Invalidate user login tokens when after a TTL


## v0.1.95 - October 13, 2025

Fixing broken login flows


## v0.1.94 - October 11, 2025

BUGFIX automated email setup


## v0.1.93 - October 11, 2025

Fixing aws email auto config


## v0.1.92 - October 11, 2025

test fails to catch syntax error


## v0.1.91 - October 11, 2025

FIXING SES Audit


## v0.1.90 - October 11, 2025

BUGFIX filestore for each user + group


## v0.1.89 - October 11, 2025

BUGFIX filemanager creating empty


## v0.1.87 - October 11, 2025

fix user upload


## v0.1.86 - October 11, 2025

Fixing file uploads for group


## v0.1.85 - October 10, 2025

group support


## v0.1.84 - October 10, 2025

simple group data


## v0.1.83 - October 10, 2025

dump all even lists


## v0.1.82 - October 10, 2025

* LOGIT_DEBUG_ALL for all logging


## v0.1.81 - October 08, 2025

Better logging


## v0.1.80 - October 08, 2025

Bugfix non str id in redis pool


## v0.1.79 - October 08, 2025

BUGfix geolocated


## v0.1.78 - October 08, 2025

* BUGFIX geoip no provider


## v0.1.77 - October 06, 2025

Syntax error tests failed


## v0.1.76 - October 06, 2025

Fixing cloud messaging mobile registration


## v0.1.75 - October 06, 2025

legacy login support debug


## v0.1.74 - October 06, 2025

Legacy login


## v0.1.73 - October 06, 2025

HOTFIX channels package removal


## v0.1.72 - October 05, 2025

Robustness of redis pools


## v0.1.71 - October 05, 2025

FIXES in aws email sending


## v0.1.70 - October 05, 2025

ADDED missing stats helper


## v0.1.69 - October 05, 2025

* mroe debug


## v0.1.68 - October 05, 2025

* trying to fix cluster bug


## v0.1.67 - October 05, 2025

* Bug fix in redis cluster mode


## v0.1.66 - October 05, 2025

* Fixes to group level permissions


## v0.1.65 - October 03, 2025

* ADDED advanced permissions via group/child/parent chaining


## v0.1.64 - October 02, 2025

* Bug in managing group members


## v0.1.63 - October 01, 2025

* ADDED ticket status changes to notes


## v0.1.62 - September 30, 2025

* Ticket bug fix


## v0.1.61 - September 30, 2025

* Fixing int fields


## v0.1.60 - September 29, 2025

* FIX no more raising redis timeout in pools


## v0.1.59 - September 28, 2025

* Bug fixes in realtime


## v0.1.58 - September 28, 2025

* more realtime logic


## v0.1.57 - September 26, 2025

* Atomic save bug


## v0.1.56 - September 26, 2025

HOTFIX atomic commits


## v0.1.55 - September 25, 2025

BUGFIX checking group member permission


## v0.1.54 - September 25, 2025

ossec fixes


## v0.1.53 - September 25, 2025

debug ossec


## v0.1.52 - September 25, 2025

* FIX ossec alerts not parsing


## v0.1.51 - September 25, 2025

* FIX password without current password


## v0.1.50 - September 25, 2025

* realtime disconnect dead connections


## v0.1.49 - September 25, 2025

* REWRITE of realtime


## v0.1.47 - September 24, 2025

debug


## v0.1.46 - September 24, 2025

debug


## v0.1.45 - September 24, 2025

debug


## v0.1.44 - September 24, 2025

* more robust error handling on channels


## v0.1.43 - September 24, 2025

* debug


## v0.1.42 - September 24, 2025

* debugging channels


## v0.1.41 - September 24, 2025

* REALTIME support


## v0.1.40 - September 24, 2025

* ADDED Channels


## v0.1.39 - September 24, 2025

* CRITICAL FIX: potential credential leakage


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