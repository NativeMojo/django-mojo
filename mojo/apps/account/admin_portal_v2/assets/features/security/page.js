import {h} from '../../core.js';
import {decodeRouteState, routeHref} from '../../components/routes.js';
import {errorState, loadingState, sectionTabs} from '../../components/views.js';
import {readSecurity} from './api.js';
import {renderCases, renderOverview} from './mojosec.js';
import {renderIncidentEvents} from './activity.js';
import {renderRules} from './rules.js';
import {renderFirewall, renderRecommendations} from './firewall.js';

const TABS = Object.freeze([
  {id: 'overview', label: 'Overview', sections: ['overview', 'schemas'], render: renderOverview},
  {id: 'cases', label: 'Cases', sections: ['cases'], render: renderCases},
  {id: 'activity', label: 'Incidents & events', sections: ['incidents', 'events'], render: renderIncidentEvents},
  {id: 'rules', label: 'Rules', sections: ['rules', 'schemas'], render: renderRules},
  {id: 'firewall', label: 'Firewall & IPSets', sections: ['ipsets', 'schemas'], render: renderFirewall},
  {id: 'recommendations', label: 'Recommendations', sections: ['recommendations', 'schemas'], render: renderRecommendations},
]);

export function tabFor() {
  const requested = decodeRouteState().state.tab;
  return TABS.find((tab) => tab.id === requested) || TABS[0];
}

function writeTab(tab) {
  history.replaceState({}, '', routeHref('security-operations', {tab}));
}

export async function securityPage(ctx, parentSignal) {
  let active = tabFor(); let controller = null; let disposed = false;
  let report = null;
  const body = h('section', {class: 'security-body'}, loadingState('Loading security…'));
  const page = h('div', {class: 'page security-page'},
    h('header', {class: 'page-header'}, h('div', {},
      h('div', {class: 'eyebrow', text: 'Security operations'}),
      h('h1', {text: 'Security', tabindex: '-1'}),
      h('p', {text: 'Complete permissioned evidence, policy and fleet enforcement truth. Authentication secrets remain hidden.'}))),
    sectionTabs({items: TABS, active: active.id, label: 'Security views', onChange: (id) => {
      active = TABS.find((tab) => tab.id === id) || TABS[0]; writeTab(active.id);
      return refresh();
    }}), body);

  const refresh = async () => {
    controller?.abort(); const current = new AbortController(); controller = current;
    if (parentSignal?.aborted) current.abort();
    body.replaceChildren(loadingState(`Loading ${active.label.toLowerCase()}…`));
    try {
      report = await readSecurity(active.sections, {signal: current.signal});
      if (disposed || current.signal.aborted || current !== controller) return;
      const node = active.render({ctx, report, refresh, signal: current.signal});
      const freshness = report.capabilities.fresh_auth;
      body.replaceChildren(h('div', {class: 'security-capability'},
        h('strong', {text: `Scope: ${report.capabilities.scope}`}),
        h('span', {text: freshness.enabled
          ? `Fresh authentication: ${freshness.window_seconds} seconds${freshness.applies_to_credential ? '' : ' (not applicable to this machine credential)'}`
          : 'Fresh authentication: disabled'})), await node);
    } catch (error) {
      if (!disposed && !current.signal.aborted && current === controller) {
        body.replaceChildren(errorState(error, refresh));
      }
    }
  };
  await refresh();
  page.dispose = () => { disposed = true; controller?.abort(); };
  return page;
}
