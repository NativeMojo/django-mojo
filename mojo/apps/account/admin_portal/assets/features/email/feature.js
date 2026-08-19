import {emailPage} from './page.js';

export default {
  id: 'email', routes: ['messaging-email'], style: 'assets/features/email/styles.css',
  enabled: (ctx) => ctx.features?.email?.enabled === true,
  // Messaging entries group only when adjacent after the order sort: SMS holds
  // 50, Email holds 51, so the two sit under one Messaging header between the
  // Control plane entries (order absent -> 0) and System Setup (order 100).
  navigation: () => [{route: 'messaging-email', label: 'Email', icon: 'mail', section: 'Messaging', order: 51}],
  title: () => 'Email',
  render: ({ctx, signal}) => emailPage(ctx, signal),
};
