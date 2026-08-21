import {placeholderPage} from '../../components/placeholder.js';

export function domainsPage(ctx) {
  return placeholderPage(ctx, {
    eyebrow: 'Domains',
    title: 'Domains',
    copy: 'Domains, DNS records, certificates, and what each name serves.',
  });
}
