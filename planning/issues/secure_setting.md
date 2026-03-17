# Request: Ability to Securely Store and Share django Settings

## Status
New

## Priority
Medium

---

## Summary

We have a major issue when it comes to storing and sharing django settings.  Many of which include secure keys.  Most of these get stored into our django-projects settings which is , or in our var/django.conf, but things like keys need to be stored and share easily and securely. We often have dozens of nodes running the same django code, and no clean way to share our settings.   


## Possible solution

I think it only makes sense that much of this is stored using encryption into redis or the database.  When django instance starts up it would update the django.settings to have these keys.  As in load all django settings from the database and override anything in django.settings.   We already do this with our own var/django.conf approach.
