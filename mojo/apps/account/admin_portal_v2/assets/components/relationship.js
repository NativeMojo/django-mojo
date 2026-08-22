import {apiEnvelope, h} from '../core.js';
import {allows} from './model.js';

function valueAt(record, path) {
  return String(path || '').split('.').filter(Boolean).reduce((value, key) => value == null ? undefined : value[key], record);
}

export class RelationshipSelect {
  constructor(options) {
    if (!options?.endpoint || !options?.name) throw new Error('RelationshipSelect needs endpoint and name');
    this.options = {...options}; this.generation = 0; this.timer = null; this.controller = null;
    this.rows = []; this.focused = -1; this.start = 0; this.count = null; this.selected = null;
    this.valuePath = options.valuePath || 'id'; this.labelPath = options.labelPath || 'name';
    this.pageSize = Math.max(1, Math.min(Number(options.pageSize || 20), 100));
    this.listId = `relationship-${crypto.randomUUID()}`;
    this.hidden = h('input', {type: 'hidden', name: options.name, value: ''});
    this.input = h('input', {type: 'text', role: 'combobox', 'aria-autocomplete': 'list', 'aria-expanded': 'false',
      'aria-controls': this.listId, autocomplete: 'off', placeholder: options.placeholder || 'Search…'});
    this.list = h('div', {id: this.listId, class: 'relationship-options', role: 'listbox', hidden: true});
    this.message = h('div', {class: 'relationship-message', role: 'status'});
    this.clearButton = h('button', {type: 'button', class: 'relationship-clear', 'aria-label': `Clear ${options.label || options.name}`, text: '×', hidden: true});
    this.node = h('div', {class: 'relationship-select'}, h('div', {class: 'relationship-input'}, this.input, this.hidden, this.clearButton), this.list, this.message);
    this.outsideClick = (event) => { if (!this.node.contains(event.target)) this._close(); };
    this._bind();
    const capable = allows(options.capability, options.context || {});
    this.input.disabled = !capable; this.clearButton.disabled = !capable;
    if (capable && options.value != null && options.value !== '') this.setValue(options.value);
    else this._select(null);
    if (!capable) this.message.textContent = options.unavailable || 'This relationship is not available with your current access.';
  }

  _bind() {
    this.input.addEventListener('focus', () => { if (!this.rows.length) this.search('', false); else this._open(); });
    this.input.addEventListener('input', () => {
      if (this.selected && this.input.value !== this._label(this.selected)) this._select(null);
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.search(this.input.value, false), Number(this.options.debounceMs || 300));
    });
    this.input.addEventListener('keydown', (event) => this._keydown(event));
    this.clearButton.addEventListener('click', () => { this._select(null); this.input.focus(); this.search('', false); });
    document.addEventListener('click', this.outsideClick);
  }

  _value(record) { return valueAt(record, this.valuePath); }
  _label(record) { return String(valueAt(record, this.labelPath) ?? this._value(record) ?? 'Unnamed'); }

  _url({search = '', start = 0, detail = null} = {}) {
    const base = detail == null ? this.options.endpoint : `${this.options.endpoint.replace(/\/$/, '')}/${encodeURIComponent(detail)}`;
    const params = new URLSearchParams();
    Object.entries(this.options.filters || {}).forEach(([key, value]) => {
      if (Array.isArray(value)) value.forEach((item) => params.append(key, item));
      else if (value != null) params.set(key, value);
    });
    if (this.options.graph) params.set('graph', this.options.graph);
    if (detail == null) { params.set('size', this.pageSize); params.set('start', start); if (search) params.set('search', search); }
    const query = params.toString(); return query ? `${base}?${query}` : base;
  }

  async setValue(value) {
    if (value && typeof value === 'object') { this._select(value); return; }
    const generation = ++this.generation; this.controller?.abort(); this.controller = new AbortController();
    this.message.textContent = 'Loading selected record…';
    try {
      const envelope = await apiEnvelope(this._url({detail: value}), {signal: this.controller.signal});
      if (generation !== this.generation) return;
      const record = envelope.data && !Array.isArray(envelope.data) ? envelope.data : envelope.items[0];
      if (!record) throw new Error('Selected record is no longer available');
      this._select(record);
    } catch (error) {
      if (error.name !== 'AbortError' && generation === this.generation) { this._select(null); this.message.textContent = error.message; }
    }
  }

  async search(term = '', append = false) {
    const generation = ++this.generation; this.controller?.abort(); this.controller = new AbortController();
    const start = append ? this.rows.length : 0; this.message.textContent = 'Searching…'; this._open();
    try {
      const envelope = await apiEnvelope(this._url({search: term.trim(), start}), {signal: this.controller.signal});
      if (generation !== this.generation) return;
      this.rows = append ? [...this.rows, ...envelope.items] : envelope.items;
      this.count = envelope.count; this.start = start; this.focused = this.rows.length ? 0 : -1;
      this.message.textContent = this.rows.length ? '' : 'No matching records.'; this._render();
    } catch (error) {
      if (error.name !== 'AbortError' && generation === this.generation) { this.rows = []; this.message.textContent = error.message; this._render(); }
    }
  }

  _render() {
    const options = this.rows.map((record, index) => {
      const id = `${this.listId}-option-${index}`;
      return h('button', {id, type: 'button', role: 'option', class: index === this.focused ? 'focused' : '',
        'aria-selected': String(this.selected && String(this._value(record)) === String(this._value(this.selected))),
        onclick: () => this._select(record), onmousemove: () => { this.focused = index; this._syncFocus(); }},
      h('strong', {text: this._label(record)}), this.options.detailPath ? h('span', {text: String(valueAt(record, this.options.detailPath) ?? '')}) : null);
    });
    const more = this.count == null ? this.rows.length >= this.pageSize : this.rows.length < this.count;
    if (more) options.push(h('button', {type: 'button', class: 'relationship-more', text: 'Load more', onclick: () => this.search(this.input.value, true)}));
    this.list.replaceChildren(...options); this.list.hidden = false; this.input.setAttribute('aria-expanded', 'true'); this._syncFocus();
  }

  _syncFocus() {
    [...this.list.querySelectorAll('[role="option"]')].forEach((node, index) => node.classList.toggle('focused', index === this.focused));
    const active = this.list.querySelectorAll('[role="option"]')[this.focused];
    if (active) { this.input.setAttribute('aria-activedescendant', active.id); active.scrollIntoView({block: 'nearest'}); }
    else this.input.removeAttribute('aria-activedescendant');
  }

  _keydown(event) {
    if (event.key === 'Escape') { this._close(); return; }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); this._open();
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      this.focused = Math.max(0, Math.min(this.rows.length - 1, this.focused + delta)); this._syncFocus();
    } else if (event.key === 'Home' && this.rows.length) { event.preventDefault(); this.focused = 0; this._syncFocus(); }
    else if (event.key === 'End' && this.rows.length) { event.preventDefault(); this.focused = this.rows.length - 1; this._syncFocus(); }
    else if (event.key === 'Enter' && this.focused >= 0) { event.preventDefault(); this._select(this.rows[this.focused]); }
  }

  _select(record) {
    this.selected = record; const value = record ? this._value(record) : '';
    this.hidden.value = value ?? ''; this.input.value = record ? this._label(record) : '';
    this.clearButton.hidden = !record || this.options.allowClear === false;
    this.input.required = Boolean(this.options.required && !record);
    this.input.setCustomValidity(this.options.required && !record ? 'Select a record.' : '');
    this.message.textContent = ''; this._close();
    this.node.dispatchEvent(new CustomEvent('relationship:change', {bubbles: true, detail: {value, record}}));
  }

  _open() { this.list.hidden = false; this.input.setAttribute('aria-expanded', 'true'); }
  _close() { this.list.hidden = true; this.input.setAttribute('aria-expanded', 'false'); this.input.removeAttribute('aria-activedescendant'); }
  getValue() { return this.hidden.value; }
  dispose() { clearTimeout(this.timer); this.controller?.abort(); document.removeEventListener('click', this.outsideClick); this._close(); }
}

export function relationshipField(options) { return {type: 'relationship', relationship: options}; }
