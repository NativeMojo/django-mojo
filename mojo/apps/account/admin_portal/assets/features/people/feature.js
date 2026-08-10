import {peoplePage} from './page.js';

export default {
  id: 'people', routes: ['users', 'groups'], style: 'assets/features/people/styles.css',
  enabled: (ctx) => ctx.features?.people?.enabled === true,
  navigation: () => [{route: 'users', matches: ['users', 'groups'], label: 'People', icon: 'users', section: 'Control plane'}],
  title: () => 'People',
  render: ({ctx, route}) => peoplePage(ctx, route),
};
