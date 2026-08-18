# Put your web app online

You know the web address you want people to type — say
`https://myapp.example.com` — and you have an app that builds into a folder of
files. This guide takes you from there to a live site with a padlock (HTTPS)
in the address bar, and then shows you how to look after it afterward.

No prior setup knowledge is assumed. You will not touch a server, and you will
not hand anyone your passwords. The wizard does the technical parts; you make a
few plain choices and, if your web address lives somewhere else, copy a line or
two into that place.

You reach the wizard in the built-in Admin portal under **Web Apps → Add**. For
the reference on the portal itself, see
[Admin Portal API Guide](../account/admin_portal.md). If you would rather wire
this into your own build pipeline instead of using GitHub, the deploy contract
is in [Releasing a site build](releases.md).

## What you need before you start

- **A web address you control.** Either a whole domain (like `example.com`) or
  a sub-address under one (like `myapp.example.com`). "Control" means you can
  sign in wherever your domain's settings live — the company you bought the
  name from, or wherever you point it today. If you do not have a name yet, the
  wizard can buy one for you.
- **Your app's files.** Anything that builds into a plain folder of files
  (an `index.html` and its assets) works. The usual path is a **GitHub
  repository** that builds your app — the wizard writes the deploy step for
  you. If you use a different build service, or want to push by hand, that
  works too; see Step 5.

That is the whole list. Now let's walk it.

## Step 1 — Type the address you want

Type the exact web address you have in mind, the way you'd say it out loud:

```
https://myapp.example.com
```

The wizard checks it right away and, if something about it won't work as a
public web address, gently steers you to the closest thing that will. You
always see the suggestion and accept it — nothing changes behind your back:

- **You typed a folder-style address** like `example.com/myapp`. Public apps
  live on their own sub-address, not on a folder, so the wizard suggests
  `myapp.example.com` instead.
- **You typed the bare domain** `example.com`. The plain domain is best left
  free to redirect or host a landing page, so the wizard suggests
  `www.example.com`.
- **You typed `http://`**. Every app here is served over secure `https://`, so
  the wizard upgrades it for you. There is no insecure option to choose.

When the address is one the wizard can serve, it tells you which of two things
is true:

- **It's ready.** Your domain is already set up here, so there is nothing to
  add — you'll go straight on.
- **A couple of records are needed.** Your domain lives somewhere else, so
  you'll add a line or two at that place in Step 3. The wizard will show you
  exactly what.

If the address is already in use by another of your apps, or the name isn't one
your account manages, the wizard says so plainly instead of guessing.

## Step 2 — Your domain

The wizard asks where your domain should live. There are three plain choices,
and the first is the one most people want:

- **Keep my DNS where it is (recommended).** Your domain stays exactly where it
  is today — you change nothing about who runs it. You'll just add a record or
  two that the wizard hands you, at your current provider, in Step 3. **No
  passwords are handed over and nothing is bought.**
- **Use a domain you've already set up.** If this workspace already runs another
  domain here, point the app at that one instead. The wizard lists the domains
  you already control, you pick one and the front part of the address, and it
  adds the record for you — nothing to copy and paste.
- **Buy a new domain.** You don't have a name yet, so the wizard finds one and
  registers it for you. You'll see the exact name and price and type them to
  confirm before anything is purchased — nothing is bought without that.

Pick **Keep my DNS where it is** unless you have a reason not to. The rest of
this guide follows that path, because it's the common one and the only one with
a copy-and-paste step.

## Step 3 — Add the records the wizard shows you

This step only appears if you kept your domain where it is. The wizard shows
you the exact record to add — usually **just one** — with the name and the
value already filled in. Your job is to copy them into your domain's settings.

The one record points your chosen address at our servers. (A second, one-time
record that proves the padlock certificate is yours is normally handled while
your domain is being set up, so most people only add the one shown here.)

1. Open a second browser tab and sign in wherever your domain's settings live —
   the company you bought the name from, or wherever you point it now.
2. Find the section usually called **DNS**, **DNS records**, or **Manage DNS**.
3. **Add a new record of type `CNAME`.** Copy the **name** and the **value**
   from the wizard exactly as shown. The name is the front part of your address
   (for `myapp.example.com`, the name is `myapp`); the value is the destination
   the wizard displays. Save it.
4. Come back to the wizard and press **I've added them — check now.**

The wizard looks up the record and, once it sees it, moves you on. New records
can take a few minutes to show up; if the check doesn't pass immediately, wait
a moment and press it again.

> **Using Cloudflare? Read this or the check will fail.** When you add the
> record, set it to **DNS only** — click the orange cloud next to the record so
> it turns grey. If the orange cloud (Cloudflare's proxy) is left on, both this
> check and your site's padlock certificate will fail to complete. Grey cloud,
> and you're fine.

You don't have to sit and wait. You can leave and come back later — the wizard
remembers where you were, and your app keeps its place until you finish or
cancel.

## Step 4 — Name your app

Give your app a friendly name so you can recognize it later — "Marketing site",
"Customer portal", whatever makes sense to you. The wizard already fills this
in from your address, so often you can just accept it.

Everything technical — which files folder your build produces, which branch
deploys, and the like — is tucked under **Advanced** with sensible defaults.
Most people never open it. If you do, the defaults match what a typical GitHub
build produces.

## Step 5 — Set up deploys

"Deploying" just means: whenever your app changes, its new files get published
to your address automatically. Pick how your files reach us.

### The GitHub way (recommended)

If your app is in a GitHub repository, this is two small additions to that
repository, and the wizard prepares both:

1. **One secret.** The wizard gives you a deploy key. In your repository's
   settings, add it as a secret named exactly **`MOJO_DEPLOY_KEY`**. A secret
   is GitHub's private store — the key lives there, never in your code, and
   nobody on your team needs to see it.
2. **One workflow file.** The wizard generates a short file to drop into your
   repository (under `.github/workflows/`). That file references our public,
   ready-made deploy step at
   `NativeMojo/django-mojo/examples/github/actions/deploy-webapp@main` and runs
   **on GitHub's own machines** — nothing is installed on your computer, and
   you don't run anything by hand.

After that, every time you push your app to GitHub, it builds and publishes
itself. You're done touching deploys.

The generated deploy key is shown to you **once**. Copy it straight into the
GitHub secret. If you lose it, you don't dig it back out — you ask for a fresh
one from the app's management screen (see below), which replaces the old one.

### The any-other-way path

Not on GitHub? Building somewhere else, or want to publish by hand? That works.
Publishing is a small, documented sequence of web requests — register the list
of files, upload each one, then say "done" — and any build service (or a script
on your own machine) can follow it. The full contract, with examples, is in
[Releasing a site build](releases.md). You still keep the same
`MOJO_DEPLOY_KEY` in whatever runs it.

## Step 6 — Go live

Press the final button and the wizard makes one real request to your address,
over `https://`, to confirm it's actually serving your app to the public. When
that comes back green, you're live: your address works, the padlock is real,
and your latest build is what visitors see.

That's it. You went from an address you had in mind to a working, secured site.

## Managing your app afterward

Open any app from **Web Apps** and you get one management screen, organized into
a few tabs. Every action explains itself in plain words before it does anything.

- **Overview.** Your address, whether it's healthy (reachable right now), and
  which build is currently live. This is your "is everything okay?" glance. You
  can re-check health on demand.
- **Deploys.** The history of every publish, newest first. Made a bad release?
  **Roll back** with one click puts an earlier, known-good build back in front
  of visitors — no rebuild, no GitHub round-trip. It takes effect right away.
- **Deploy key.** The `MOJO_DEPLOY_KEY` from Step 5. If it ever leaks, or you
  just want a fresh one, **rotate** it here. The old key stops working the
  instant the new one is made, so update your GitHub secret with the new value
  right after.
- **Setup.** Need the workflow file or the key again — say you're wiring up a
  second repository, or you lost the key? This re-shows the generated workflow
  and can mint a fresh key for you.
- **Danger.** The changes you should make deliberately, each spelled out before
  it runs:
  - **Change the address.** Move the app to a different web address. Your
    current address keeps serving visitors until the new one is fully ready and
    secured, then the old one is retired — no gap where the site is down.
  - **Take offline.** Stop serving the address while keeping the app and all
    its build history. You can put it back later.
  - **Delete.** Remove the app entirely. Its serving setup and deploy key are
    torn down together, cleanly.

## Adding a second app to the same domain

Once a domain is set up here, putting **another** app on it is nearly instant,
because each app lives on its own sub-address (`shop.example.com`,
`docs.example.com`, and so on) under the same domain and the same padlock.

- If you kept your DNS elsewhere, you add just **one** record for the new
  sub-address — the certificate side is already covered from the first time.
- If your domain is one we run here already, there's **nothing to add at all** —
  type the new address and go straight to naming and deploys.

So the first app on a domain is the only one that asks for the full walk. Every
app after that is a couple of clicks.

## See also

- [Releasing a site build](releases.md) — the deploy contract for GitHub and
  any other build service, including rollback and key rotation.
- [Admin Portal API Guide](../account/admin_portal.md) — the portal these
  screens live in, and the permissions each action needs.
- [edge API reference](README.md) — the API index behind all of this,
  including the endpoint-by-endpoint WebApp reference.
