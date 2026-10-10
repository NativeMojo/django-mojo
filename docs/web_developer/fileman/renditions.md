# Rendition Options — REST API Reference

How an admin UI reads and changes the sizes, formats and codecs fileman uses
when it builds thumbnails, previews and transcodes, and which of those run
automatically after an upload.

## Endpoints

| Method | Path | Authority |
|---|---|---|
| `GET` | `/api/fileman/renditions/options` | any of `manage_settings`, `manage_files`, `files`, `groups` |
| `GET` / `POST` | `/api/settings` | the generic settings API (the `Setting` model); `manage_settings` globally, or `groups` within a group |

There is no dedicated write endpoint. The options are three ordinary
settings rows, written through `/api/settings` like any other setting, and
the server validates them on save.

## Reading the options

**GET** `/api/fileman/renditions/options`

```json
{
  "status": true,
  "data": {
    "categories": {
      "video": {
        "key": "FILEMAN_RENDITIONS_VIDEO",
        "defaults": {
          "thumbnail": {"width": 300, "height": 169, "time_offset": "00:00:03", "format": "jpg"},
          "video_preview": {"width": 640, "height": 360, "bitrate": "500k", "duration": 10, "format": "mp4", "codec": "h264", "audio": true},
          "video_mp4": {"width": 1280, "height": 720, "format": "mp4", "codec": "h265", "crf": 28, "preset": "medium", "audio": true},
          "video_webm": {"width": 1280, "height": 720, "bitrate": "2000k", "format": "webm", "audio": true}
        },
        "automatic_default": ["video_thumbnail", "thumbnail", "video_preview", "video_mp4"],
        "override": null,
        "effective": {
          "roles": {"...": "defaults with the override applied"},
          "automatic": ["video_thumbnail", "thumbnail", "video_preview"]
        },
        "role_kinds": {"thumbnail": "thumbnail", "video_thumbnail": "thumbnail", "video_preview": "transcode", "...": "..."},
        "fields": {
          "thumbnail": ["format", "height", "time_offset", "width"],
          "transcode": ["audio", "bitrate", "codec", "crf", "duration", "format", "height", "preset", "width"]
        },
        "choices": {
          "thumbnail": {"format": ["jpg", "png"]},
          "transcode": {"format": ["mp4", "webm"], "codec": ["h264", "h265"], "preset": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"]}
        },
        "limits": {"width": [1, 4096], "height": [1, 4096], "crf": [18, 51], "duration": [1, 60]}
      },
      "image": {"...": "same shape; one kind, image"},
      "document": {"...": "same shape; kinds thumbnail and pdf"}
    },
    "engine": {"key": "FILEMAN_VIDEO_ENGINE", "choices": ["ffmpeg"], "effective": "ffmpeg"}
  }
}
```

| Field | Meaning |
|---|---|
| `key` | The settings key to write for this category. |
| `defaults` | The server's built-in options per role. Show these greyed when there is no override; "reset to defaults" means clearing the override. |
| `automatic_default` | Which roles run after an upload when nothing is set. |
| `override` | The global settings row's current value, parsed, or `null` when none exists. Only a database row is reported here — a value configured in the deployment's settings file is applied but cannot be cleared from the UI, so it is not shown as an override. |
| `effective` | `defaults` with the override applied, and the effective automatic list. |
| `role_kinds` | Each role's kind, which selects its `fields` and `choices`. |
| `fields` | The option names a role of that kind accepts. Anything else is refused on save. |
| `choices` | Allowed values for the enum options, per kind. |
| `limits` | Inclusive `[min, max]` for the numeric options this category uses. Clamp inputs to these; the server refuses anything outside them. |
| `engine` | The transcode backend. Only `ffmpeg` exists today; the key is reserved for a managed engine later. |

Group-scoped overrides are not reported by this endpoint; it describes the
global view. Read a group's row through `/api/settings?group=<id>`.

## Writing the options

Write the category's whole value as one JSON object to its `key`. Name only
the roles and options you change — everything else keeps its default. The
optional `_automatic` list replaces the set of roles that run after an upload.

**POST** `/api/settings` (create) or **POST** `/api/settings/<id>` (update)

```json
{
  "key": "FILEMAN_RENDITIONS_VIDEO",
  "value": {
    "video_mp4": {"crf": 24, "preset": "fast"},
    "_automatic": ["thumbnail", "video_thumbnail", "video_preview", "video_mp4", "video_webm"]
  }
}
```

Add `"group": <id>` to scope the row to one group (and its descendants);
omit it for the global row. The value may be sent as an object or as a JSON
string.

A bad value is refused with HTTP 400 and a message that names the field:

```json
{"status": false, "error": "video_mp4.crf: must be between 18 and 51"}
```

Refused: an unknown role; an unknown option for that role's kind; a number
outside its `limits`; a value not in `choices`; `codec`, `crf` or `preset`
on a role whose `format` is `webm`; `_automatic` naming an unknown role; a
value that is not a JSON object. Render the message next to the field it
names rather than as a generic toast.

**Format wins over codec.** Changing a role's `format` to `webm` is accepted
even though its default carries `codec: "h265"`; the codec is simply ignored
for VP8. Setting `codec: "h265"` on a role whose format is `webm` is refused.

To clear an override, save `{}` as the row's value (settings rows cannot be
deleted through the API; `DELETE /api/settings/<id>` is refused). An empty
object means "defaults", and the options endpoint reports it as
`"override": null`. The next upload or re-render uses the defaults again.

## Seeing the result

Changing the options does not touch existing files. Re-render one file with
the `regenerate_renditions` action on it — see
[Regenerating renditions](files.md#regenerating-renditions) — and read back
its `renditions` map. `video_mp4` is H.265 by default and runs on upload; it
plays in Safari, Chrome and Edge but not Firefox. A deployment that needs an
H.264 file sets `{"video_mp4": {"codec": "h264", "bitrate": "2000k"}}`; one
that needs a Firefox fallback adds `video_webm` to `_automatic`.
