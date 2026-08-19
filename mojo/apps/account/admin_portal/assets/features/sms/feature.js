import {smsPage} from './sms.js';

export default {
  id: 'sms', routes: ['messaging-sms'], style: 'assets/features/sms/styles.css',
  enabled: (ctx) => ctx.features?.sms?.enabled === true,
  // Messaging entries group only when adjacent after the order sort: SMS holds
  // 50 and the sibling Email feature holds 51, between the Control plane
  // entries (order absent -> 0) and System Setup (order 100).
  navigation: () => [{route: 'messaging-sms', label: 'Text messages', icon: 'phone', section: 'Messaging', order: 50}],
  title: () => 'Text messages',
  render: ({ctx, signal}) => smsPage(ctx, signal),
};
