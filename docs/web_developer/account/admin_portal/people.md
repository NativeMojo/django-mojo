# Admin People

People provides searchable Users and Groups. Selecting a row opens the standard
right inspector rather than navigating to an arbitrary model editor.

User sections cover identity and verification, lifecycle, invitation/reset,
temporary password, session revocation, seven high-level access bundles,
stored sign-in evidence, and links to related Activity lanes. Group sections
cover identity and searchable parent selection, members and roles, API-key
lifecycle, permissions, related Activity, and read-only Advanced metadata.

Temporary passwords and newly created or rotated API-key tokens are shown once.
Copy them before closing the dialog; closing clears the input and in-memory
value. They must not be copied into URLs, browser storage, telemetry, or
downloads. The portal does not use API-key `graph=token` recovery.

Sensitive mutations may answer HTTP `440 reauth_required` when the current
interactive authentication is older than 600 seconds. Return through the
Bouncer reauthentication flow and repeat the intentional action. API keys and
group-scoped tokens cannot perform these actions.

Activity links use only this stable query vocabulary:
`tab,start,size,sort,search,date,user,group,ip,hostname,incident,model,model_id`.
The Activity feature separately authorizes each lane; a visible People record
does not grant access to its Logs, Events, Incidents, or Tickets.
