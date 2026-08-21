import {homePage} from './page.js';

// Home is the installation's own destination: its dot is the worst state
// anywhere inside the portal. It stays enabled on the same block v1's Dashboard
// uses, so a caller who can see one can see the other.
export default {
  id: 'home',
  routes: ['home'],
  style: 'assets/features/home/styles.css',
  enabled: (ctx) => ctx.features?.dashboard?.enabled === true,
  navigation: () => [{route: 'home', label: 'Home', icon: 'home'}],
  title: () => 'Home',
  render: ({ctx, navigate}) => homePage(ctx, navigate),
};
