import {infrastructurePage, visibleTabs} from './page.js';

// Infrastructure carries v1's Fleet Scaling, Metrics and Maintenance as tabs
// of one destination. All three live behind the platform block, each behind
// its own capability — so the destination is offered only when at least one of
// its tabs is readable. A caller with the platform block but none of the three
// grants (a superuser without manage_aws, whose only platform route in v1 is
// System Setup) gets no entry rather than an entry that refuses.
export default {
  id: 'infrastructure',
  routes: ['infrastructure'],
  style: 'assets/features/infrastructure/styles.css',
  enabled: (ctx) => ctx.features?.platform?.enabled === true
    && visibleTabs(ctx).length > 0,
  navigation: () => [{route: 'infrastructure', label: 'Infrastructure', icon: 'server'}],
  title: () => 'Infrastructure',
  // The signal is the page's own abort: Metrics and Maintenance poll, and a
  // route change must stop those polls. The Capacity batch is deliberately
  // NOT on this signal — it belongs to the global operation store and keeps
  // being watched after the operator leaves.
  render: ({ctx, navigate, signal}) => infrastructurePage({ctx, navigate, signal}),
};
