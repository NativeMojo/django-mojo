// The honest placeholder.
//
// v2 ships one destination at a time. A destination whose screen is not built
// yet says exactly that and sends the operator to the portal that does have it.
// It never renders an empty shell, a "coming soon" teaser, or a disabled
// control — a screen that looks built and does nothing is worse than no screen.
//
// Nothing has moved: v1 is untouched and still serves everything it always did.

import {h, pageHeader} from '../core.js';

export function placeholderPage(ctx, {eyebrow, title, copy}) {
  const adminPath = ctx.admin_path || '/admin/';
  return h('div', {class: 'page'},
    pageHeader(eyebrow, title, copy),
    h('section', {class: 'panel'},
      h('div', {class: 'panel-head'}, h('h2', {text: 'Not built in v2 yet'})),
      h('div', {class: 'panel-body'},
        h('p', {text: `This section isn't built in v2 yet. Use the current Admin at ${adminPath} — nothing has moved.`}),
        h('div', {},
          h('a', {class: 'button primary', href: adminPath}, `Open the current Admin`)))));
}
