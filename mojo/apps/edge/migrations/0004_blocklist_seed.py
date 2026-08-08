# The blocklist seed. CreateModel is Django-generated; the RunPython imports
# django-mojo-skeleton's file-managed sec.d content as DATA, in **log** mode —
# the log-first posture: every imported rule observes (the edge watch log)
# until a human flips it to enforce.
#
# What the skeleton shipped, and how it lands here:
#   - aws/nginx/sec.d/badbot.conf: `~*^Lynx 0;` (an ALLOW exception — Lynx
#     user agents contain `libwww-FM`, which the unanchored `libwww` token
#     matches; allow rows render FIRST in their maps, which is what keeps the
#     exception effective) -> one row, mode=allow. `libwww-perl` exact and
#     ONE ~2,635-character case-insensitive alternation -> SPLIT PER TOKEN,
#     one `ua` row per token (each well under the 256-char cap), mode=log.
#     The `(?i)` wrapper is dropped: rows render as `~*` matches, which are
#     already case-insensitive. One duplicate token (^TurnitinBot) is seeded
#     once — the model is unique on (kind, value).
#   - aws/nginx/sec.d/chrome.conf: HeadlessChrome + the two legacy-Chrome
#     version patterns -> mode=log.
#   - aws/nginx/sec.d/blocked_ips.conf: EMPTY (comments only) -> zero ip rows.
#
# The RunPython calls validate_blocklist_entry EXPLICITLY per row: historical
# models drop custom save(), and seeding unvalidated values would bypass the
# exact whitelist this table exists to enforce.
#
# Reverse: deletes rows carrying this migration's note marker, nothing else.

import mojo.models.rest
from django.db import migrations, models


SEED_NOTE = "seed: django-mojo-skeleton sec.d"

# The badbot.conf alternation, one token per row.
BADBOT_TOKENS = (
    "80legs", "360Spider", "Aboundex", "Abonti",
    "Acunetix", "^AIBOT", "^Alexibot", "Alligator",
    "AllSubmitter", "Apexoo", "^asterias", "^attach",
    "^BackDoorBot", "^BackStreet", "^BackWeb", "Badass",
    "Bandit", "Baid", "Baiduspider", "^BatchFTP",
    "^Bigfoot", "^Black.Hole", "^BlackWidow", "BlackWidow",
    "^BlowFish", "Blow", "^BotALot", "Buddy",
    "^BuiltBotTough", "^Bullseye", "^BunnySlippers", "BBBike",
    "^Cegbfeieh", "^CheeseBot", "^CherryPicker", "^ChinaClaw",
    "^Cogentbot", "CPython", "Collector", "cognitiveseo",
    "Copier", "^CopyRightCheck", "^cosmos", "^Crescent",
    "CSHttp", "^Custo", "^Demon", "^Devil",
    "^DISCo", "^DIIbot", "discobot", "^DittoSpyder",
    "Download.Demon", "Download.Devil", "Download.Wonder", "^dragonfly",
    "^Drip", "^eCatch", "^EasyDL", "^ebingbong",
    "^EirGrabber", "^EmailCollector", "^EmailSiphon", "^EmailWolf",
    "^EroCrawler", "^Exabot", "^Express", "Extractor",
    "^EyeNetIE", "FHscan", "^FHscan", "^flunky",
    "^Foobot", "^FrontPage", "GalaxyBot", "^gotit",
    "Grabber", "^GrabNet", "^Grafula", "^Harvest",
    "^HEADMasterSEO", "^hloader", "^HMView", "^HTTrack",
    "httrack", "HTTrack", "htmlparser", "^humanlinks",
    "^IlseBot", "Image.Stripper", "Image.Sucker", "imagefetch",
    "^InfoNaviRobot", "^InfoTekies", "^Intelliseek", "^InterGET",
    "^Iria", "^Jakarta", "^JennyBot", "^JetCar",
    "JikeSpider", "^JOC", "^JustView", "^Jyxobot",
    "^Kenjin.Spider", "^Keyword.Density", "libwww", "^larbin",
    "LeechFTP", "LeechGet", "^LexiBot", "^lftp",
    "^libWeb", "^likse", "^LinkextractorPro", "^LinkScan",
    "^LNSpiderguy", "^LinkWalker", "msnbot", "MSIECrawler",
    "MJ12bot", "MegaIndex", "^Magnet", "^Mag-Net",
    "^MarkWatch", "Mass.Downloader", "masscan", "^Mata.Hari",
    "^Memo", "^MIIxpc", "^NAMEPROTECT", "^Navroad",
    "^NearSite", "^NetAnts", "^Netcraft", "^NetMechanic",
    "^NetSpider", "^NetZIP", "^NextGenSearchBot", "^NICErsPRO",
    "^niki-bot", "^NimbleCrawler", "^Nimbostratus-Bot", "^Ninja",
    "^Nmap", "nmap", "^NPbot", "Offline.Explorer",
    "Offline.Navigator", "OpenLinkProfiler", "^Octopus", "^Openfind",
    "^OutfoxBot", "Pixray", "probethenet", "proximic",
    "^PageGrabber", "^pavuk", "^pcBrowser", "^Pockey",
    "^ProPowerBot", "^ProWebWalker", "^psbot", "^Pump",
    "^QueryN.Metasearch", "^RealDownload", "Reaper", "^Reaper",
    "^Ripper", "Ripper", "Recorder", "^ReGet",
    "^RepoMonkey", "^RMA", "scanbot", "SEOkicks-Robot",
    "seoscanners", "^Stripper", "^Sucker", "Siphon",
    "Siteimprove", "^SiteSnagger", "SiteSucker", "^SlySearch",
    "^SmartDownload", "^Snake", "^Snapbot", "^Snoopy",
    "Sosospider", "^sogou", "spbot", "^SpaceBison",
    "^spanner", "^SpankBot", "Spinn4r", "^Sqworm",
    "Sqworm", "Stripper", "Sucker", "^SuperBot",
    "SuperHTTP", "^SuperHTTP", "^Surfbot", "^suzuran",
    "^Szukacz", "^tAkeOut", "^Teleport", "^Telesoft",
    "^TurnitinBot", "^The.Intraformant", "^TheNomad", "^TightTwatBot",
    "^Titan", "^True_Robot", "^turingos", "^URLy.Warning",
    "^Vacuum", "^VCI", "VidibleScraper", "^VoidEYE",
    "^WebAuto", "^WebBandit", "^WebCopier", "^WebEnhancer",
    "^WebFetch", "^Web.Image.Collector", "^WebLeacher", "^WebmasterWorldForumBot",
    "WebPix", "^WebReaper", "^WebSauger", "Website.eXtractor",
    "^Webster", "WebShag", "^WebStripper", "WebSucker",
    "^WebWhacker", "^WebZIP", "Whack", "Whacker",
    "^Widow", "Widow", "WinHTTrack", "^WISENutbot",
    "WWWOFFLE", "^WWWOFFLE", "^WWW-Collector-E", "^Xaldon",
    "^Xenu", "^Zade", "^Zeus", "ZmEu",
    "^Zyborg", "SemrushBot", "^WebFuck", "^MJ12bot",
    "^majestic12", "^WallpapersHD",
)

CHROME_PATTERNS = (
    r"HeadlessChrome",
    r"Chrome/[1-9][0-9]\..*",
    r"Chrome/(10[0-2])\..*",
)


def seed_blocklist(apps, schema_editor):
    from mojo.apps.edge.validators import validate_blocklist_entry

    BlocklistEntry = apps.get_model("edge", "BlocklistEntry")

    rows = [("ua", "^Lynx", "allow")]
    rows.append(("ua", "libwww-perl", "log"))
    rows.extend(("ua", token, "log") for token in BADBOT_TOKENS)
    rows.extend(("ua", pattern, "log") for pattern in CHROME_PATTERNS)

    seen = set()
    for kind, value, mode in rows:
        if (kind, value) in seen:
            continue
        seen.add((kind, value))
        entry = BlocklistEntry(kind=kind, value=value, mode=mode,
                               note=SEED_NOTE)
        # Historical models have no save() override — validate explicitly.
        validate_blocklist_entry(entry)
        entry.save()


def unseed_blocklist(apps, schema_editor):
    BlocklistEntry = apps.get_model("edge", "BlocklistEntry")
    BlocklistEntry.objects.filter(note=SEED_NOTE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('edge', '0003_vhost_kinds_routes'),
    ]

    operations = [
        migrations.CreateModel(
            name='BlocklistEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('modified', models.DateTimeField(auto_now=True, db_index=True)),
                ('kind', models.CharField(choices=[('ip', 'IP address or CIDR network'), ('ua', 'User-agent regex pattern')], default='ip', help_text='ip | ua', max_length=8)),
                ('value', models.CharField(help_text='ip: address or CIDR, stored normalized. ua: whitelisted regex fragment, matched case-insensitively.', max_length=512)),
                ('mode', models.CharField(choices=[('allow', 'Exempt from blocking AND watching'), ('off', 'Kept, rendered nowhere'), ('log', 'Watched: logged to the edge watch log, never blocked'), ('enforce', 'Blocked with 444')], db_index=True, default='log', help_text='allow | off | log | enforce', max_length=8)),
                ('note', models.CharField(blank=True, default='', help_text='Why this row exists. The seed migration marks its rows here.', max_length=255)),
            ],
            options={
                'db_table': 'edge_blocklist_entry',
                'ordering': ['kind', 'value'],
                'constraints': [models.UniqueConstraint(fields=('kind', 'value'), name='edge_blocklist_kind_value_uniq')],
            },
            bases=(models.Model, mojo.models.rest.MojoModel),
        ),
        migrations.RunPython(seed_blocklist, unseed_blocklist),
    ]
