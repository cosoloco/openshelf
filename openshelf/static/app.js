'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#icon-${name}"/></svg>`;
const number = value => Number(value || 0).toLocaleString();
const authors = value => Array.isArray(value) ? value.join(' & ') : value || 'Unknown author';
const bytes = value => {
  if (value == null) return 'Size unknown';
  let size = Number(value), unit = 0;
  const units = ['B','KB','MB','GB','TB'];
  while (size >= 1000 && unit < units.length - 1) { size /= 1000; unit++; }
  return `${size.toLocaleString(undefined, {maximumFractionDigits:unit > 1 ? 1 : 0})} ${units[unit]}`;
};
const languageNames = {eng:'English',fra:'French',deu:'German',spa:'Spanish',ita:'Italian',por:'Portuguese',nld:'Dutch',jpn:'Japanese',zho:'Chinese',rus:'Russian',und:'Unspecified',pol:'Polish',swe:'Swedish',fin:'Finnish',dan:'Danish',nor:'Norwegian',ces:'Czech',ukr:'Ukrainian',hin:'Hindi',ara:'Arabic'};
const lang = value => languageNames[value] || value;
const date = value => value ? new Date(value).toLocaleDateString(undefined, {month:'short',day:'numeric',year:'numeric'}) : 'Not yet indexed';
const statusNames = {new:'Not indexed',queued:'Queued',indexing:'Indexing',ready:'Indexed',error:'Needs attention',paused:'Paused',disabled:'Disabled',running:'Downloading',complete:'Complete',failed:'Failed',cancelled:'Cancelled'};
const badge = value => `<span class="status-badge ${esc(value)}">${esc(statusNames[value] || value)}</span>`;
const cover = (id, title = '') => `<img src="/cover/${encodeURIComponent(id)}" data-cover="${esc(id)}" alt="${esc(title ? 'Cover of ' + title : '')}" loading="lazy" decoding="async">`;

let csrf = $('meta[name="csrf-token"]').content;
let meta = null, state = {}, result = null, currentBook = null, renderVersion = 0;
let selected = new Set(), sourcesData = [], serverFilter = '', serverQuery = '', downloadFilter = '';
let queueRequest = null, toastTimer, searchTimer, importTimer, polling = false;
const advancedLabels = {author:'Author',title:'Title',series_query:'Series',subject:'Subject',publisher:'Publisher',year_from:'Year from',year_to:'Year to',identifier:'Identifier',identifier_type:'Identifier type',series_from:'Series number from',series_to:'Series number to',description:'Description',library:'Library',saved:'Saved only',downloaded:'Downloaded only'};
const filterKeys = ['q','q_field','text_match','language','tag','format','source','series',...Object.keys(advancedLabels)];
let advancedOpen = false, advancedSignature = '', advancedDirty = false, searchPending = false;
let view = 'grid';
try { view = localStorage.getItem('openshelf-view') || 'grid'; } catch (_) {}

async function api(path, method = 'GET', data) {
  const options = {method, credentials:'same-origin', headers:{}};
  if (method !== 'GET') {
    options.headers = {'Content-Type':'application/json','X-CSRF-Token':csrf};
    options.body = JSON.stringify(data || {});
  }
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({error:'Open Shelf could not complete the request. Please try again.'}));
  if (!response.ok) throw new Error(body.error || 'The request failed.');
  return body;
}

function toast(message, error = false) {
  const node = $('#toast');
  const modal = $('dialog[open]');
  (modal || document.body).append(node);
  node.textContent = message;
  node.classList.toggle('error', error);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 4500);
}

function closeMenu() {
  $('#sidebar').classList.remove('open');
  $('#nav-shade').hidden = true;
  $('#mobile-toggle').setAttribute('aria-expanded','false');
}

function readRoute() {
  const [page, query = ''] = location.hash.slice(1).split('?');
  const params = Object.fromEntries(new URLSearchParams(query));
  return {...params, mode:['catalog','series','saved','servers','downloads','settings'].includes(page) ? page : 'catalog', page:Math.max(1, Number(params.page) || 1)};
}

function navigate(changes, reset = false) {
  clearTimeout(searchTimer);
  searchPending = false;
  const next = {...(reset ? {} : state), ...changes};
  const mode = next.mode || 'catalog';
  const params = new URLSearchParams();
  for (const [key,value] of Object.entries(next)) {
    if (key !== 'mode' && value !== '' && value != null && !(key === 'page' && Number(value) === 1)) params.set(key,value);
  }
  const hash = '#' + mode + (params.size ? '?' + params : '');
  if (hash === location.hash) render(); else location.hash = hash;
}

function bookFilters() {
  const filters = {};
  for (const key of [...filterKeys,'sort']) if (state[key]) filters[key] = state[key];
  if (state.mode === 'saved') { filters.saved = 'true'; filters.availability = 'all'; }
  return filters;
}

function searchHelp() {
  const mode = $('#advanced-text-match').value;
  $('#advanced-match-help').textContent = ({words:'All words matches complete words in any order. Author words must belong to the same author.',phrase:'Exact phrase matches consecutive words in the order you enter them.',exact:'Exact field value matches the whole title, one author name, one subject, or other text field.'})[mode] + ' Field matching ignores capitalization, accents, and punctuation. Keywords also search descriptions. The filters above still apply.';
}

function syncAdvancedSearch(catalogView) {
  $('#search-scope').hidden = !catalogView;
  $('#search-scope').value = state.q_field || 'all';
  $('#advanced-toggle').hidden = !catalogView;
  const active = Object.keys(advancedLabels).filter(key => state[key]);
  const matching = state.text_match && state.text_match !== 'words';
  const activeCount = active.length + (matching ? 1 : 0);
  const signature = JSON.stringify([...active.map(key => [key,state[key]]),['text_match',state.text_match || 'words']]);
  const changed = signature !== advancedSignature;
  if (changed && activeCount) advancedOpen = true;
  advancedSignature = signature;
  $('#advanced-form').hidden = !catalogView || !advancedOpen;
  $('#advanced-toggle').setAttribute('aria-expanded',String(catalogView && advancedOpen));
  $('#advanced-count').hidden = !activeCount;
  $('#advanced-count').textContent = activeCount;
  if (changed || !advancedDirty) {
    for (const key of [...Object.keys(advancedLabels),'text_match']) {
      const input = $('#advanced-form').elements.namedItem(key);
      if (input.type === 'checkbox') input.checked = state[key] === 'true' || (key === 'saved' && state.mode === 'saved');
      else input.value = state[key] || (key === 'text_match' ? 'words' : '');
    }
    advancedDirty = false;
  }
  $('#advanced-saved').disabled = state.mode === 'saved';
  $('#advanced-error').textContent = '';
  searchHelp();
  const chips = [];
  const labels = {q:({author:'Author',title:'Title',series_query:'Series'})[state.q_field] || 'Keywords',...advancedLabels,series:'Series (exact)',language:'Language',tag:'Subject (exact)',format:'Format',source:'Server',text_match:'Matching'};
  for (const [key,label] of Object.entries(labels)) {
    if (!state[key] || (key === 'text_match' && !matching)) continue;
    let value = key === 'language' ? lang(state[key]) : key === 'source' ? meta.sources.find(s => String(s.id) === state[key])?.name || state[key] : state[key];
    if (key === 'text_match') value = state[key] === 'exact' ? 'Exact field value' : 'Exact phrase';
    const text = ['saved','downloaded'].includes(key) ? label : `${label}: ${value}`;
    chips.push(`<button class="filter-chip" data-remove-filter="${key}" title="Remove ${esc(text)}" aria-label="Remove ${esc(text)}">${esc(text)}${icon('close')}</button>`);
  }
  $('#active-filters').hidden = !catalogView || !chips.length;
  $('#active-filters').innerHTML = chips.join('');
}

function setOptions(id, items, value, label) {
  const node = $(id);
  const options = [`<option value="">${esc(label)}</option>`, ...items.map(([v,t]) => `<option value="${esc(v)}">${esc(t)}</option>`)];
  if (value && !items.some(([v]) => String(v) === String(value))) options.push(`<option value="${esc(value)}">${esc(value)}</option>`);
  node.innerHTML = options.join('');
  node.value = value || '';
}

async function refreshMeta() {
  meta = await api('/api/bootstrap');
  csrf = meta.csrf;
  $('#nav-books').textContent = number(meta.books);
  $('#nav-series').textContent = number(meta.series);
  $('#nav-saved').textContent = number(meta.saved);
  $('#nav-servers').textContent = number(meta.sources.length);
  const downloading = (meta.downloads.running || 0) + (meta.downloads.queued || 0);
  $('#nav-downloads').textContent = number(downloading || meta.downloads.complete || 0);
  const indexing = (meta.scans.running || 0) + (meta.scans.queued || 0);
  $('#scan-banner').hidden = !indexing;
  $('#scan-banner-text').textContent = meta.settings.indexing_paused ? `Indexing paused · ${number(indexing)} servers waiting` : `Building your catalog · ${number(meta.scans.running || 0)} indexing, ${number(meta.scans.queued || 0)} waiting`;
  $('#sidebar-activity').textContent = indexing ? `${number(meta.books)} books discovered` : meta.sources.length ? `${number(meta.sources.filter(s => s.status === 'ready').length)} servers indexed` : 'Ready to connect';
  $('#footer-status').textContent = `${number(meta.books)} books · ${number(meta.sources.length)} connections`;
  setOptions('#language-filter', meta.languages.map(r => [r.language,lang(r.language)]), state.language, 'All languages');
  setOptions('#tag-filter', meta.tags.map(r => [r.tag,r.tag]), state.tag, 'All subjects');
  setOptions('#format-filter', meta.formats.map(r => [r.format,r.format]), state.format, 'All formats');
  setOptions('#source-filter', meta.sources.map(r => [r.id,r.name]), state.source, 'All servers');
  return meta;
}

function stat(label, value, symbol, suffix = '') {
  return `<div class="stat-card"><div class="stat-label">${icon(symbol)}${esc(label)}</div><div class="stat-value">${esc(value)}${suffix ? `<small>${esc(suffix)}</small>` : ''}</div></div>`;
}

function renderStats() {
  const ready = meta.sources.filter(s => s.status === 'ready').length;
  $('#stats').innerHTML = stat('BOOKS DISCOVERED',number(meta.books),'book') + stat('CONNECTED SERVERS',number(meta.sources.length),'server',`${number(ready)} indexed`) + stat('SERIES TO EXPLORE',number(meta.series),'series') + stat('BOOKS DOWNLOADED',number(meta.downloads.complete),'download');
}

function empty(title, description, action = '', steps = false) {
  return `<div class="empty-state"><div class="empty-illustration" aria-hidden="true"><span>${icon('save')}</span><span>${icon('book')}</span><span>${icon('series')}</span></div><h2>${esc(title)}</h2><p>${esc(description)}</p>${action}${steps ? '<div class="empty-steps"><span><b>01</b> Connect libraries</span><span><b>02</b> Explore the catalog</span><span><b>03</b> Take a book home</span></div>' : ''}</div>`;
}

function pagination(data) {
  $('#pagination').innerHTML = data.pages > 1 ? `<button data-go-page="${data.page-1}" ${data.page <= 1 ? 'disabled' : ''}>Previous</button><span>Page ${number(data.page)} of ${number(data.pages)}</span><button data-go-page="${data.page+1}" ${data.page >= data.pages ? 'disabled' : ''}>Next</button>` : '';
}

function card(book) {
  book.formats = book.formats || [];
  const series = book.series ? `<div class="book-series">${esc(book.series)}${book.series_position != null ? ' · '+esc(book.series_position) : ''}</div>` : '';
  const fmt = book.formats.includes(meta.settings.preferred_format) ? meta.settings.preferred_format : book.formats[0] || '—';
  return `<article class="book-card ${selected.has(book.id) ? 'selected' : ''}" data-id="${esc(book.id)}">
    <label class="book-select"><input type="checkbox" data-select="${esc(book.id)}" ${selected.has(book.id) ? 'checked' : ''} aria-label="Select ${esc(book.title)}"></label>
    <button class="save-button ${book.saved ? 'saved' : ''}" data-save="${esc(book.id)}" data-saved="${book.saved}" aria-label="${book.saved ? 'Unsave' : 'Save'} ${esc(book.title)}" aria-pressed="${book.saved}">${icon('save')}</button>
    <button class="book-cover" data-book="${esc(book.id)}" aria-label="View ${esc(book.title)}">${cover(book.id,book.title)}${book.downloaded ? `<span class="downloaded-label">${icon('check')}Downloaded</span>` : ''}</button>
    <div class="book-info"><button class="book-title" data-book="${esc(book.id)}">${esc(book.title)}</button><p class="book-author">${esc(authors(book.authors))}</p>${series}
    <div class="book-foot"><div><span class="format-pill">${esc(fmt)}</span><span class="source-caption">${number(book.source_count)} ${book.source_count === 1 ? 'source' : 'sources'}</span></div><button class="download-quick" data-download="${esc(book.id)}" aria-label="Download ${esc(book.title)}" title="Add to Downloads" ${!book.formats.length ? 'disabled' : ''}>${icon(book.downloaded ? 'check' : 'download')}</button></div></div>
  </article>`;
}

function selectionBar() {
  if (!result?.books?.length) { $('#results-buttons').innerHTML = ''; return; }
  const all = result.books.every(b => selected.has(b.id));
  $('#results-buttons').innerHTML = `<label><input id="select-page" type="checkbox" ${all ? 'checked' : ''}>Select page</label>${selected.size ? `<button class="quiet-button" data-action="queue-selected">${icon('download')}Download ${number(selected.size)} selected</button><button class="quiet-button" data-action="clear-selection">Clear</button>` : `<button class="quiet-button" data-action="queue-results">${icon('download')}Download results</button>`}`;
}

async function renderBooks(version) {
  const params = new URLSearchParams({...bookFilters(),page:state.page});
  const data = await api('/api/books?' + params);
  if (version !== renderVersion) return;
  result = data;
  $('#content').className = 'book-grid' + (view === 'rows' ? ' rows' : '');
  $('#results-count').innerHTML = `<strong>${number(data.total)}</strong> ${state.mode === 'saved' ? 'saved books' : 'books'}${state.q ? ` matching “${esc(state.q)}”` : ''}`;
  if (data.books.length) $('#content').innerHTML = data.books.map(card).join('');
  else if (!meta.sources.length) $('#content').innerHTML = empty('A whole shelf of possibilities.','Connect the libraries you use. Their books become one searchable catalog, ready to explore.',`<button class="button primary" data-action="import">${icon('plus')}Connect your first server</button>`,true);
  else if (!meta.books && ((meta.scans.running || 0) + (meta.scans.queued || 0))) $('#content').innerHTML = empty('Your catalog is taking shape.','Books will appear here as your servers respond. You can follow each connection as it is indexed.','<a class="button secondary" href="#servers">Watch the progress '+icon('arrow')+'</a>');
  else if (state.mode === 'saved' && !Object.keys(bookFilters()).some(k => !['saved','availability'].includes(k))) $('#content').innerHTML = empty('Keep a few for later.','Use the bookmark on a book to make a shortlist of your next reads.','<a class="button secondary" href="#catalog">Explore the catalog '+icon('arrow')+'</a>');
  else $('#content').innerHTML = empty('Nothing on this shelf yet.','Try a different search or clear your filters. You can also check your servers for indexing errors.',`<button class="button secondary" data-action="reset">Clear filters</button>`);
  selectionBar();
  pagination(data);
}

async function renderSeries(version) {
  const data = await api('/api/series?' + new URLSearchParams({q:state.q || '',page:state.page}));
  if (version !== renderVersion) return;
  $('#content').className = 'series-grid';
  $('#results-count').innerHTML = `<strong>${number(data.total)}</strong> series · members known to your catalog`;
  $('#content').innerHTML = data.series.length ? data.series.map(s => `<button class="series-card" data-series="${esc(s.name)}"><div class="series-covers">${s.covers.map(id => cover(id)).join('')}</div><h3>${esc(s.name)}</h3><p>${number(s.books)} ${s.books === 1 ? 'book' : 'books'} indexed <span aria-hidden="true">→</span></p></button>`).join('') : empty('Find stories that go further.','Series appear when your connected libraries supply series metadata.');
  pagination(data);
}

function sourceRows() {
  const rows = sourcesData.filter(s => (!serverFilter || s.status === serverFilter) && (!serverQuery || (s.name+' '+s.url).toLowerCase().includes(serverQuery.toLowerCase())));
  const wrap = $('#source-rows');
  if (!wrap) return;
  wrap.innerHTML = rows.length ? `<div class="table-wrap"><table class="source-table"><thead><tr><th>SERVER</th><th>STATUS</th><th>BOOKS</th><th>LAST INDEXED</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${rows.map(s => {
    const busy = ['queued','indexing'].includes(s.status);
    return `<tr><td><a class="server-name" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.name)}</a><span class="server-url">${esc(s.protocol ? (s.protocol === 'calibre' ? 'Calibre Content Server' : 'OPDS catalog') : s.url)}</span>${s.error ? `<span class="server-error">${esc(s.error)}</span>` : ''}</td><td>${badge(s.status)}${s.status === 'indexing' ? `<span class="server-progress">${number(s.processed)}${s.total != null ? ' / '+number(s.total) : ''} records</span>${s.total ? `<progress value="${s.processed}" max="${s.total}" aria-label="Indexing progress"></progress>` : ''}` : ''}</td><td class="numeric">${number(s.records)}</td><td><span class="server-progress">${esc(date(s.last_success))}</span></td><td><div class="table-actions">${busy ? `<button class="icon-button" data-source-action="pause" data-source-id="${s.id}" title="Pause indexing" aria-label="Pause ${esc(s.name)}">${icon('pause')}</button>` : `<button class="icon-button" data-source-action="resume" data-source-id="${s.id}" title="${s.status === 'ready' ? 'Refresh catalog' : 'Index / resume'}" aria-label="Index ${esc(s.name)}">${icon('refresh')}</button>`}${s.records ? `<button class="icon-button" data-browse-source="${s.id}" title="Browse this server" aria-label="Browse ${esc(s.name)}">${icon('book')}</button>` : ''}${s.enabled && !busy ? `<button class="icon-button" data-source-action="disable" data-source-id="${s.id}" title="Disable server" aria-label="Disable ${esc(s.name)}">${icon('pause')}</button>` : ''}${!busy ? `<button class="icon-button" data-remove-source="${s.id}" title="Remove server" aria-label="Remove ${esc(s.name)}">${icon('close')}</button>` : ''}</div></td></tr>`;
  }).join('')}</tbody></table></div>` : empty('No matching servers.','Adjust the filter or add another connection.');
  $('#results-count').innerHTML = `<strong>${number(rows.length)}</strong> ${rows.length === 1 ? 'server' : 'servers'}${serverFilter ? ' · '+esc(statusNames[serverFilter]) : ''}`;
}

async function renderSources(version, refreshOnly = false) {
  const data = await api('/api/sources');
  if (version !== renderVersion || state.mode !== 'servers') return;
  sourcesData = data.sources;
  $('#content').className = 'workspace';
  if (!data.sources.length) {
    $('#content').innerHTML = empty('Your libraries, connected.','Add a single Calibre server, an OPDS feed, or a list of addresses. Index them together and browse everything in one place.',`<button class="button primary" data-action="import">${icon('plus')}Import servers</button>`);
    $('#results-count').textContent = 'No servers connected';
    return;
  }
  if (!refreshOnly || !$('#source-rows')) $('#content').innerHTML = `<div class="workspace-tools"><div class="status-tabs" id="server-tabs"></div><input class="server-search" id="server-search" type="search" placeholder="Filter servers…" aria-label="Filter servers" value="${esc(serverQuery)}"></div><div id="source-rows"></div>`;
  $('#server-tabs').innerHTML = [['','All'],['indexing','Indexing'],['queued','Queued'],['ready','Indexed'],['error','Needs attention'],['paused','Paused']].map(([key,label]) => `<button data-server-filter="${key}" class="${serverFilter === key ? 'active' : ''}">${label}<span>${number(key ? data.counts[key] || 0 : data.sources.length)}</span></button>`).join('');
  sourceRows();
}

async function renderDownloads(version) {
  const data = await api('/api/downloads?' + new URLSearchParams({status:downloadFilter,page:state.page}));
  if (version !== renderVersion || state.mode !== 'downloads') return;
  $('#content').className = 'workspace';
  $('#results-count').innerHTML = `<strong>${number(data.counts.complete)}</strong> complete · ${bytes(data.complete_bytes)} saved`;
  const buttons = d => {
    let out = '';
    if (['running','queued'].includes(d.status)) out += `<button class="icon-button" data-job-action="pause" data-job-id="${d.id}" title="Pause" aria-label="Pause ${esc(d.title)}">${icon('pause')}</button>`;
    if (d.status === 'paused') out += `<button class="icon-button" data-job-action="resume" data-job-id="${d.id}" title="Resume" aria-label="Resume ${esc(d.title)}">${icon('play')}</button>`;
    if (['failed','cancelled'].includes(d.status)) out += `<button class="icon-button" data-job-action="retry" data-job-id="${d.id}" title="Retry" aria-label="Retry ${esc(d.title)}">${icon('refresh')}</button>`;
    if (['queued','running','paused'].includes(d.status)) out += `<button class="icon-button" data-job-action="cancel" data-job-id="${d.id}" title="Cancel" aria-label="Cancel ${esc(d.title)}">${icon('close')}</button>`;
    if (d.status === 'complete') out += `<a class="icon-button" href="/api/downloads/${d.id}/file" title="Save another copy in this browser" aria-label="Save a browser copy of ${esc(d.title)}">${icon('download')}</a>`;
    return out;
  };
  const rows = data.downloads.map(d => `<article class="download-row">${cover(d.book_id)}<div><button class="download-title" data-book="${esc(d.book_id)}">${esc(d.title)}</button><p class="download-author">${esc(d.authors)}${d.format ? ' · '+esc(d.format) : ''}</p>${d.error ? `<p class="download-error">${esc(d.error)}</p>` : ''}</div><div class="download-progress">${badge(d.status)}${['running','paused'].includes(d.status) ? `<progress ${d.total ? `value="${d.bytes}" max="${d.total}"` : ''} aria-label="Download progress"></progress>` : ''}<div><span>${bytes(d.bytes)}${d.total && d.status !== 'complete' ? ' / '+bytes(d.total) : ''}</span>${d.status === 'complete' ? `<span>${esc(date(d.finished_at))}</span>` : ''}</div></div><div class="download-actions">${buttons(d)}</div></article>`).join('');
  $('#content').innerHTML = `<div class="download-folder">${icon('folder')}<span>${esc(data.settings.download_dir)}</span><a class="quiet-button" href="#settings">Change folder</a></div><div class="workspace-tools"><div class="status-tabs">${[['','All'],['running','Active'],['queued','Queued'],['complete','Complete'],['failed','Failed'],['paused','Paused']].map(([key,label]) => `<button data-download-filter="${key}" class="${key === downloadFilter ? 'active' : ''}">${label}<span>${number(key ? data.counts[key] || 0 : Object.values(data.counts).reduce((a,b) => a+b,0))}</span></button>`).join('')}</div></div>${rows ? `<div class="download-list">${rows}</div>` : empty('Make room for a good book.','Choose books from the catalog and they’ll download to your folder. Then import them into Calibre or open them in your favorite reader.','<a href="#catalog" class="button secondary">Browse the catalog '+icon('arrow')+'</a>')}`;
  pagination(data);
}

async function renderSettings(version) {
  const data = await api('/api/settings');
  if (version !== renderVersion) return;
  $('#content').className = 'workspace';
  $('#content').innerHTML = `<form id="settings-form" class="settings-panel"><h2>Give your books a place to land.</h2><p class="intro">Open Shelf discovers and downloads. Your favorite reader can take it from there.</p><label class="field-label" for="setting-folder">Download folder</label><input class="text-input" id="setting-folder" name="download_dir" value="${esc(data.download_dir)}" required><p class="field-note">A folder on the computer running Open Shelf. In Docker, use the folder mounted inside the container, usually /downloads.</p><p class="free-space">${bytes(data.free_bytes)} available on this drive</p><label class="field-label" for="setting-format">Preferred book format</label><select id="setting-format" name="preferred_format">${['EPUB','AZW3','MOBI','PDF','FB2','TXT','DJVU','CBZ','DOCX','RTF','ODT','HTMLZ'].map(f => `<option ${f === data.preferred_format ? 'selected' : ''}>${f}</option>`).join('')}</select><p class="field-note">Try this format first, then an alternative if it isn’t available. You can choose an exact format for individual downloads.</p><div class="settings-pair"><div><label class="field-label" for="setting-reserve">Keep free on disk (GB)</label><input class="text-input" id="setting-reserve" name="reserve_gb" type="number" min="0.25" max="10000" step="0.25" value="${data.reserve_gb}" required></div><div><label class="field-label" for="setting-size">Maximum file size (MB)</label><input class="text-input" id="setting-size" name="max_file_mb" type="number" min="1" max="10240" value="${data.max_file_mb}" required></div></div><p class="field-note">Files beyond these limits are reported in Downloads. Completed files remain in your folder.</p><p class="form-error" id="settings-error" role="alert"></p><button class="button primary" type="submit">${icon('check')}Save settings</button></form>`;
}

function updateHeading() {
  const headings = {
    catalog:['YOUR CONNECTED LIBRARIES','The catalog','Good books are out there. Bring them into view.'],
    saved:['A LITTLE SOMETHING FOR LATER','Saved books','A shortlist of stories worth coming back to.'],
    series:['ONE GOOD BOOK LEADS TO ANOTHER','Follow the story','Explore the series found across your connected libraries.'],
    servers:['BRING YOUR LIBRARIES TOGETHER','Your connections','Import servers, follow indexing, and keep your catalog up to date.'],
    downloads:['TAKE YOUR NEXT READ WITH YOU','Downloads','From a remote shelf to a folder of your own.'],
    settings:['MAKE YOURSELF AT HOME','Your preferences','A few small choices for the way you collect books.'],
  };
  const [eyebrow,title,subtitle] = headings[state.mode];
  $('#page-eyebrow').textContent = state.series ? 'THE NEXT CHAPTER' : eyebrow;
  $('#page-title').textContent = state.series || title;
  $('#page-subtitle').textContent = state.series ? 'Series members known to your connected catalogs.' : subtitle;
  let actions = '';
  if (state.mode === 'servers') actions = `<button class="button secondary small-button" data-action="toggle-indexing">${icon(meta.settings.indexing_paused ? 'play' : 'pause')}${meta.settings.indexing_paused ? 'Resume' : 'Pause'} indexing</button> <button class="button primary" data-action="import">${icon('plus')}Import servers</button>`;
  else if (state.mode === 'downloads') actions = `<button class="button secondary" data-action="toggle-downloads">${icon(meta.settings.downloads_paused ? 'play' : 'pause')}${meta.settings.downloads_paused ? 'Resume' : 'Pause'} downloads</button>`;
  else if (state.series) actions = '<a href="#series" class="quiet-button">All series '+icon('arrow')+'</a>';
  $('#page-actions').innerHTML = actions;
  const catalogView = ['catalog','saved'].includes(state.mode);
  $('#stats').hidden = state.mode !== 'catalog' || !!state.series;
  $('#search-section').hidden = !['catalog','saved','series'].includes(state.mode);
  $('#catalog-filters').hidden = !catalogView;
  $('#results-bar').hidden = state.mode === 'settings';
  $('#view-toggle').hidden = !catalogView;
  if (!searchPending) $('#search').value = state.q || '';
  $('#search').placeholder = state.mode === 'series' ? 'Search series…' : ({author:'Search author names…',title:'Search book titles…',series_query:'Search series names…'})[state.q_field] || 'Search titles, authors, subjects, or descriptions…';
  syncAdvancedSearch(catalogView);
  for (const key of ['language','tag','format','source']) {
    const node = $('#' + key + '-filter');
    const value = state[key] || '';
    if (value && !Array.from(node.options).some(o => o.value === value)) node.add(new Option(value,value));
    node.value = value;
  }
  const seriesSort = $('#sort option[value="series"]');
  const seriesOrder = state.series || state.series_query || state.sort === 'series';
  if (seriesOrder && !seriesSort) $('#sort').add(new Option('Series order','series'));
  if (!seriesOrder && seriesSort) seriesSort.remove();
  $('#sort').value = state.sort || 'title';
  $('#reset-filters').hidden = !filterKeys.some(key => state[key]);
  for (const node of $$('.nav-link')) node.classList.toggle('active',node.dataset.page === state.mode);
  for (const button of $$('[data-view]')) { button.classList.toggle('active',button.dataset.view === view); button.setAttribute('aria-pressed',button.dataset.view === view); }
}

async function render() {
  const version = ++renderVersion;
  const previousMode = state.mode;
  state = readRoute();
  if (state.mode !== previousMode) advancedDirty = false;
  selected.clear();
  closeMenu();
  $('#pagination').innerHTML = '';
  $('#results-buttons').innerHTML = '';
  $('#content').setAttribute('aria-busy','true');
  try {
    if (!meta) await refreshMeta();
    updateHeading(); renderStats();
    $('#results-count').textContent = 'Opening this shelf…';
    if (['catalog','saved'].includes(state.mode)) await renderBooks(version);
    else if (state.mode === 'series') await renderSeries(version);
    else if (state.mode === 'servers') {
      $('#results-buttons').innerHTML = `<button class="quiet-button" data-action="scan-all">${icon('refresh')}Refresh all</button><button class="quiet-button" data-action="retry-servers">Retry unavailable</button><a class="quiet-button" href="/api/sources/export">Export list</a>`;
      await renderSources(version);
    } else if (state.mode === 'downloads') await renderDownloads(version);
    else if (state.mode === 'settings') await renderSettings(version);
  } catch (error) {
    if (version === renderVersion) { $('#content').className = 'workspace'; $('#content').innerHTML = `<div class="error-message">${esc(error.message)}</div>`; $('#results-count').textContent = 'Unable to load'; }
  } finally {
    if (version === renderVersion) $('#content').setAttribute('aria-busy','false');
  }
}

async function openBook(id, reopen = true) {
  const dialog = $('#book-dialog');
  if (reopen) { $('#book-detail').innerHTML = '<div class="loading">Turning to the details…</div>'; if (!dialog.open) dialog.showModal(); }
  const book = await api('/api/books/' + encodeURIComponent(id));
  book.formats = book.formats || [];
  currentBook = book;
  const active = book.downloads.find(d => ['queued','running','paused'].includes(d.status));
  const completed = book.downloads.find(d => d.status === 'complete');
  $('#book-detail').innerHTML = `<div class="book-detail-layout"><aside class="detail-aside">${cover(book.id,book.title).replace('<img ','<img class="detail-cover" ')}<select id="book-format" aria-label="Download format"><option value="auto">Prefer ${esc(meta.settings.preferred_format)}</option>${book.formats.map(f => `<option value="${esc(f)}">${esc(f)} only</option>`).join('')}</select><button class="button primary" data-download="${esc(book.id)}" data-detail="true" ${active || !book.formats.length ? 'disabled' : ''}>${icon('download')}${active ? esc(statusNames[active.status]) : 'Add to Downloads'}</button>${completed ? `<a class="quiet-button" href="/api/downloads/${completed.id}/file">${icon('check')}Save a browser copy</a>` : ''}<button class="quiet-button" data-save="${esc(book.id)}" data-saved="${book.saved}">${icon('save')}${book.saved ? 'Saved for later' : 'Save for later'}</button><p class="field-note">Saved to your configured download folder.</p></aside><div class="detail-main"><div class="detail-heading"><div class="eyebrow">${esc(lang(book.language))}${book.year ? ' · '+book.year : ''}</div><h2 id="book-title">${esc(book.title)}</h2><p class="detail-authors">${esc(authors(book.authors))}</p><div class="detail-tags">${book.tags.slice(0,12).map(t => `<button class="tag-button" data-tag="${esc(t)}">${esc(t)}</button>`).join('')}</div>${book.series ? `<button class="detail-series-link" data-series="${esc(book.series)}">${esc(book.series)}${book.series_position != null ? ' · Book '+esc(book.series_position) : ''} →</button>` : ''}</div><section class="detail-section"><h3>ABOUT THIS BOOK</h3><div class="book-description">${esc(book.description || 'This source has not supplied a description.')}</div></section><section class="detail-section"><h3>${number(book.copies.length)} INDEXED ${book.copies.length === 1 ? 'SOURCE' : 'SOURCES'}</h3>${book.copies.map(c => `<div class="source-card"><div class="source-card-head"><a href="${esc(c.url)}" target="_blank" rel="noopener noreferrer">${esc(c.name)} ${icon('external')}</a>${badge(c.active ? c.status : 'disabled')}</div><small>${c.files.map(f => esc(f.format)+(f.size ? ' · '+bytes(f.size) : '')).join(' &nbsp; / &nbsp; ') || 'No supported download format'}<br>Last seen ${esc(date(c.last_seen))}${c.library ? ' · '+esc(c.library) : ''}</small></div>`).join('')}</section><p class="detail-facts">${esc(book.publisher)}${book.year ? ' · '+book.year : ''}<br>Metadata comes from your connected libraries. Availability is checked when downloading.</p></div></div>`;
}

function openQueue(kind) {
  if (!result) return;
  const count = kind === 'selected' ? selected.size : result.total;
  if (!count) return;
  queueRequest = kind === 'selected' ? {ids:[...selected]} : {filters:bookFilters()};
  $('#queue-summary').textContent = `Queue ${number(count)} ${count === 1 ? 'book' : 'books'}${kind === 'selected' ? ' you selected' : ' matching the current catalog filters'}?`;
  $('#queue-destination').textContent = meta.settings.download_dir;
  $('#queue-error').textContent = '';
  $('#queue-format').value = state.format || 'auto';
  $('#queue-dialog').showModal();
}

async function act(event) {
  const node = event.target.closest('button,a');
  if (!node) return;
  try {
    if (node.dataset.close) { $('#' + node.dataset.close).close(); return; }
    if (node.dataset.book) { await openBook(node.dataset.book); return; }
    if (node.dataset.series) { $$('dialog[open]').forEach(d => d.close()); navigate({mode:'catalog',series:node.dataset.series,sort:'series'},true); return; }
    if (node.dataset.tag) { $$('dialog[open]').forEach(d => d.close()); navigate({mode:'catalog',tag:node.dataset.tag},true); return; }
    if (node.dataset.removeFilter) { clearTimeout(searchTimer); navigate({[node.dataset.removeFilter]:'',page:1}); return; }
    if (node.dataset.save) {
      const saved = node.dataset.saved !== 'true';
      await api('/api/books/' + node.dataset.save + '/save','POST',{saved});
      if (currentBook?.id === node.dataset.save && $('#book-dialog').open) await openBook(node.dataset.save,false);
      for (const button of $$(`[data-save="${node.dataset.save}"]`)) { button.dataset.saved = String(saved); button.classList.toggle('saved',saved); button.setAttribute('aria-pressed',String(saved)); }
      await refreshMeta(); toast(saved ? 'Saved for later.' : 'Removed from saved books.');
      if (state.mode === 'saved' && !$('#book-dialog').open) await renderBooks(renderVersion);
      return;
    }
    if (node.dataset.download) {
      const format = node.dataset.detail ? $('#book-format').value : 'auto';
      const response = await api('/api/downloads','POST',{ids:[node.dataset.download],format});
      toast(response.queued ? 'Added to Downloads.' : 'This book is already downloaded, queued, or unavailable in that format.');
      if (node.dataset.detail) await openBook(node.dataset.download,false);
      await refreshMeta(); return;
    }
    if (node.dataset.goPage) { navigate({page:Number(node.dataset.goPage)}); window.scrollTo({top:0,behavior:'smooth'}); return; }
    if (node.dataset.view) { view = node.dataset.view; try { localStorage.setItem('openshelf-view',view); } catch (_) {} updateHeading(); $('#content').classList.toggle('rows',view === 'rows'); return; }
    if (node.dataset.browseSource) { navigate({mode:'catalog',source:node.dataset.browseSource},true); return; }
    if (node.dataset.sourceAction) { await api(`/api/sources/${node.dataset.sourceId}/action`,'POST',{action:node.dataset.sourceAction}); await refreshMeta(); await renderSources(renderVersion,true); return; }
    if (node.dataset.removeSource) {
      const source = sourcesData.find(item => String(item.id) === node.dataset.removeSource);
      if (!source || !window.confirm(`Remove ${source.name}?\n\nThis removes the server and its indexed catalog records. Books supplied by another connected server stay available. Downloaded files in your chosen folder are not deleted.`)) return;
      const response = await api(`/api/sources/${node.dataset.removeSource}`,'DELETE');
      await refreshMeta();
      await renderSources(renderVersion,true);
      toast(`${response.removed} removed${response.orphaned ? `; ${number(response.orphaned)} catalog ${response.orphaned === 1 ? 'book' : 'books'} removed` : ''}.`);
      return;
    }
    if (node.dataset.serverFilter != null) { serverFilter = node.dataset.serverFilter; await renderSources(renderVersion,true); return; }
    if (node.dataset.downloadFilter != null) { downloadFilter = node.dataset.downloadFilter; state.page = 1; await renderDownloads(renderVersion); return; }
    if (node.dataset.jobAction) { await api(`/api/downloads/${node.dataset.jobId}/action`,'POST',{action:node.dataset.jobAction}); await renderDownloads(renderVersion); return; }
    const action = node.dataset.action;
    if (action === 'import') { $('#import-error').textContent = ''; $('#import-dialog').showModal(); $('#server-text').focus(); }
    if (action === 'reset') navigate({mode:state.mode,page:1},true);
    if (action === 'queue-selected') openQueue('selected');
    if (action === 'queue-results') openQueue('results');
    if (action === 'clear-selection') { selected.clear(); $$('.book-card').forEach(c => c.classList.remove('selected')); $$('[data-select]').forEach(c => c.checked = false); selectionBar(); }
    if (action === 'scan-all' || action === 'retry-servers') { const r = await api('/api/sources/scan','POST',action === 'retry-servers' ? {failed_only:true} : {restart:true}); toast(`${number(r.queued)} servers queued for indexing.`); await refreshMeta(); await renderSources(renderVersion,true); }
    if (action === 'toggle-indexing' || action === 'toggle-downloads') { const key = action === 'toggle-indexing' ? 'indexing_paused' : 'downloads_paused'; await api('/api/settings','PATCH',{[key]:!meta.settings[key]}); await refreshMeta(); updateHeading(); }
  } catch (error) { toast(error.message,true); }
}

document.addEventListener('click',act);
window.addEventListener('hashchange',() => { refreshMeta().then(render).catch(e => toast(e.message,true)); });
$('#mobile-toggle').addEventListener('click',() => { const open = $('#sidebar').classList.toggle('open'); $('#nav-shade').hidden = !open; $('#mobile-toggle').setAttribute('aria-expanded',String(open)); });
$('#nav-shade').addEventListener('click',closeMenu);
$('#search').addEventListener('input',() => { clearTimeout(searchTimer); searchPending = true; searchTimer = setTimeout(() => navigate({q:$('#search').value,page:1}),300); });
$('#search-scope').addEventListener('change',() => { clearTimeout(searchTimer); navigate({q:$('#search').value,q_field:$('#search-scope').value === 'all' ? '' : $('#search-scope').value,page:1}); });
$('#advanced-toggle').addEventListener('click',() => {
  advancedOpen = !advancedOpen;
  $('#advanced-form').hidden = !advancedOpen;
  $('#advanced-toggle').setAttribute('aria-expanded',String(advancedOpen));
  if (advancedOpen) $('#advanced-author').focus();
});
$('#advanced-text-match').addEventListener('change',searchHelp);
$('#advanced-form').addEventListener('input',() => { advancedDirty = true; });
$('#advanced-form').addEventListener('change',() => { advancedDirty = true; });
$('#clear-advanced').addEventListener('click',() => {
  clearTimeout(searchTimer);
  advancedDirty = false;
  const cleared = Object.fromEntries([...Object.keys(advancedLabels),'text_match'].map(key => [key,'']));
  navigate({...cleared,page:1});
});
$('#advanced-form').addEventListener('submit',event => {
  event.preventDefault();
  clearTimeout(searchTimer);
  const data = Object.fromEntries(new FormData(event.target));
  for (const key of Object.keys(advancedLabels)) data[key] = (data[key] || '').trim();
  for (const [from,to,label] of [['year_from','year_to','Publication year'],['series_from','series_to','Series number']]) {
    if (data[from] && data[to] && Number(data[from]) > Number(data[to])) { $('#advanced-error').textContent = `${label}: the minimum cannot exceed the maximum.`; return; }
  }
  if (data.text_match === 'words') data.text_match = '';
  advancedDirty = false;
  navigate({...data,q:$('#search').value,q_field:$('#search-scope').value === 'all' ? '' : $('#search-scope').value,page:1});
});
$('#reset-filters').addEventListener('click',() => navigate({mode:state.mode,page:1},true));
for (const [id,key] of [['#language-filter','language'],['#tag-filter','tag'],['#format-filter','format'],['#source-filter','source'],['#sort','sort']]) $(id).addEventListener('change',() => navigate({[key]:$(id).value,page:1}));
document.addEventListener('keydown',event => { if (event.key === '/' && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName) && !$('dialog[open]') && !$('#search-section').hidden) { event.preventDefault(); $('#search').focus(); } });
document.addEventListener('change',event => {
  if (event.target.dataset.select) { const id = event.target.dataset.select; event.target.checked ? selected.add(id) : selected.delete(id); event.target.closest('.book-card').classList.toggle('selected',event.target.checked); selectionBar(); }
  if (event.target.id === 'select-page') { const checked = event.target.checked; for (const b of result.books) checked ? selected.add(b.id) : selected.delete(b.id); $$('[data-select]').forEach(c => { c.checked = selected.has(c.dataset.select); c.closest('.book-card').classList.toggle('selected',c.checked); }); selectionBar(); }
});
document.addEventListener('input',event => { if (event.target.id === 'server-search') { serverQuery = event.target.value; sourceRows(); } });
for (const dialog of $$('dialog')) dialog.addEventListener('click',event => { if (event.target === dialog) { const r = dialog.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) dialog.close(); } });

$('#server-text').addEventListener('input',() => {
  clearTimeout(importTimer);
  importTimer = setTimeout(async () => {
    try { const data = await api('/api/sources/preview','POST',{text:$('#server-text').value}); $('#import-preview').textContent = `${number(data.urls.length)} addresses found${data.existing ? ' · '+number(data.existing)+' already imported' : ''}${data.invalid.length ? ' · '+number(data.invalid.length)+' invalid' : ''}`; }
    catch (error) { $('#import-preview').textContent = error.message; }
  },300);
});
$('#server-file').addEventListener('change',async event => { const file = event.target.files[0]; if (!file) return; if (file.size > 2_000_000) { $('#import-error').textContent = 'Choose a file smaller than 2 MB.'; return; } $('#server-text').value = await file.text(); $('#server-text').dispatchEvent(new Event('input')); });
$('#import-form').addEventListener('submit',async event => {
  event.preventDefault(); $('#import-submit').disabled = true; $('#import-error').textContent = '';
  try { const response = await api('/api/sources/import','POST',{text:$('#server-text').value,index:$('#index-after-import').checked}); $('#import-dialog').close(); $('#server-text').value = ''; $('#server-file').value = ''; $('#import-preview').textContent = 'HTTP and HTTPS addresses supported'; await refreshMeta(); navigate({mode:'servers'},true); toast(`${number(response.added)} servers imported${response.existing ? '; '+number(response.existing)+' already present' : ''}.`); }
  catch (error) { $('#import-error').textContent = error.message; }
  finally { $('#import-submit').disabled = false; }
});
$('#queue-form').addEventListener('submit',async event => {
  event.preventDefault(); $('#queue-submit').disabled = true; $('#queue-error').textContent = '';
  try { const response = await api('/api/downloads','POST',{...queueRequest,format:$('#queue-format').value}); $('#queue-dialog').close(); selected.clear(); await refreshMeta(); navigate({mode:'downloads'},true); toast(`${number(response.queued)} books queued${response.skipped ? '; '+number(response.skipped)+' skipped' : ''}.`); }
  catch (error) { $('#queue-error').textContent = error.message; }
  finally { $('#queue-submit').disabled = false; }
});
document.addEventListener('submit',async event => {
  if (event.target.id !== 'settings-form') return;
  event.preventDefault(); const submit = $('button[type=submit]',event.target); submit.disabled = true;
  try { const data = Object.fromEntries(new FormData(event.target)); data.reserve_gb = Number(data.reserve_gb); data.max_file_mb = Number(data.max_file_mb); await api('/api/settings','PATCH',data); await refreshMeta(); toast('Preferences saved.'); $('#settings-error').textContent = ''; }
  catch (error) { $('#settings-error').textContent = error.message; }
  finally { submit.disabled = false; }
});

async function refreshCovers() {
  const images = $$('img[data-cover]:not([data-ready])').filter(img => img.getBoundingClientRect().bottom > 0 && img.getBoundingClientRect().top < innerHeight + 150).slice(0,80);
  if (!images.length) return;
  const data = await api('/api/covers?' + new URLSearchParams({ids:[...new Set(images.map(i => i.dataset.cover))].join(',')}));
  const ready = new Set(data.ready);
  for (const image of images) if (ready.has(image.dataset.cover)) { image.dataset.ready = 'true'; image.src = '/cover/' + image.dataset.cover + '?ready=1'; }
}

setInterval(async () => {
  if (polling || document.hidden || !meta) return;
  polling = true;
  try {
    const oldCount = meta.books;
    await refreshMeta(); renderStats();
    if (state.mode === 'servers') await renderSources(renderVersion,true);
    if (state.mode === 'downloads') await renderDownloads(renderVersion);
    if (['catalog','saved'].includes(state.mode) && oldCount !== meta.books && !selected.size && !$('dialog[open]')) await renderBooks(renderVersion);
    await refreshCovers();
  } catch (_) { /* A temporary disconnect does not discard the user's current view. */ }
  finally { polling = false; }
},5000);

render();
