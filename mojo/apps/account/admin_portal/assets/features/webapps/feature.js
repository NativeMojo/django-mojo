import {webappsPage} from './page.js';

export default {
  id: 'webapps', routes: ['webapps'], style: 'assets/features/webapps/styles.css',
  enabled: (ctx) => ctx.features?.webapps?.enabled === true,
  navigation: () => [{route: 'webapps', label: 'WebApps', icon: 'deploy', section: 'Applications'}],
  title: () => 'WebApps',
  render: ({ctx}) => webappsPage(ctx),
};
