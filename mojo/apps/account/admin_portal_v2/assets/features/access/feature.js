import {accessPage} from './page.js';

// Access merges v1's People lane with its security-activity views, so either
// block is enough to open the destination.
export default {
  id: 'access',
  routes: ['access'],
  style: 'assets/features/access/styles.css',
  enabled: (ctx) => ctx.features?.people?.enabled === true
    || ctx.features?.activity?.enabled === true,
  navigation: () => [{route: 'access', label: 'Access', icon: 'users'}],
  title: () => 'Access',
  render: ({ctx}) => accessPage(ctx),
};
