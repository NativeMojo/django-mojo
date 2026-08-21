import {domainsPage} from './page.js';

// Domains is v1's Domains & DNS plus Certificates and Serving — every one of
// them a reader of the advanced block.
export default {
  id: 'domains',
  routes: ['domains'],
  style: 'assets/features/domains/styles.css',
  enabled: (ctx) => ctx.features?.advanced?.enabled === true,
  navigation: () => [{route: 'domains', label: 'Domains', icon: 'globe'}],
  title: () => 'Domains',
  render: ({ctx}) => domainsPage(ctx),
};
