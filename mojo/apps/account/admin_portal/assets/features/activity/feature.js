import {h} from '../../core.js';
import {emptyState} from '../../components/views.js';

export default {
  id: 'activity', routes: ['activity'], style: 'assets/features/activity/styles.css',
  enabled: (ctx) => ctx.features?.activity?.enabled === true,
  navigation: () => [{route: 'activity', label: 'Activity', icon: 'activity', section: 'Control plane'}],
  title: () => 'Activity',
  render: () => h('div', {class: 'page'}, emptyState('Activity is not enabled for this installation.')),
};
