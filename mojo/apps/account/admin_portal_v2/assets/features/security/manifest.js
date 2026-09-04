import {securityPage, tabFor} from './page.js';

export default {
  id: 'security',
  routes: ['security-operations'],
  style: 'assets/features/security/styles.css',
  enabled: (ctx) => ctx.features?.security?.enabled === true
    && ctx.features.security.capabilities?.view === true,
  navigation: () => [{
    route: 'security-operations', label: 'Security', icon: 'lock',
    section: 'Control plane', order: 50,
  }],
  title: () => tabFor().label,
  render: ({ctx, signal}) => securityPage(ctx, signal),
};
