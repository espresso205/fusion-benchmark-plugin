/* Static content stays readable without JavaScript, HTTP or WebGL. */
(() => {
  'use strict';
  const assetBase = new URL('.', document.currentScript.src);
  const data = JSON.parse(document.getElementById('report-data').textContent);
  const rows = data.cases || [];
  const sections = [...document.querySelectorAll('.case-detail')];
  const cards = [...document.querySelectorAll('.case-card')];
  const overview = document.getElementById('overview');
  const angles = {iso: 'ISO · 等轴', front: 'Front · 前视', top: 'Top · 俯视', right: 'Right · 右视'};
  const online = ['http:', 'https:'].includes(location.protocol);
  let active = -1, frame = 'scoring', mode = 'split', angle = 'iso', customView = false;
  let viewer = null, token = 0, abort = null, viewClass = null, staticReason = '';
  let lightboxItems = [], lightboxIndex = 0, lightboxTrigger = null;
  const lightbox = document.getElementById('image-lightbox');

  function destroyViewer() {
    ++token;
    abort?.abort(); abort = null;
    if (viewer) { viewer.dispose(); viewer = null; }
    document.querySelectorAll('.viewer-mount').forEach(host => host.replaceChildren());
  }

  function resourceURL(value) {
    if (typeof value !== 'string' || !value || /[\\:?#%]/.test(value) || value.startsWith('/') || value.split('/').some(part => !part || part === '.' || part === '..')) return null;
    return new URL(value, document.baseURI).href;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function hasDifference(display) {
    return ['ok', 'completed', 'ready', 'partial', 'empty'].includes(display?.difference?.status);
  }

  function hasStaticDifference(display) {
    const preview = display?.metadata?.preview;
    return hasDifference(display) && preview?.overlay_kind !== 'ordinary_overlay' && preview?.overlay_fallback !== 'ordinary_overlay';
  }

  function differenceLabel(display, staticImage = false) {
    if (staticImage && !hasStaticDifference(display)) return '透明叠加';
    if (!hasDifference(display)) return '透明叠加';
    return display.difference.scope === 'interface' ? 'Interface 区域差异' : '全模型差异';
  }

  function differenceLegend(display, staticImage = false) {
    const holder = element('div');
    if (staticImage && hasDifference(display) && !hasStaticDifference(display)) {
      holder.append(element('p', 'difference-note', '此处为普通透明叠加；差异着色可在三维模式查看。')); return holder;
    }
    if (hasDifference(display)) {
      const legend = element('div', 'difference-legend');
      for (const [key, label] of Object.entries({matched: '匹配', extra: '多余', missing: '缺失'})) legend.append(element('span', key, label));
      holder.append(legend);
      const explanation = display.difference.scope === 'interface' ? '依据提供的 KOR / KIR 区域显示，不代表 Interface 评分。' : '三角网格的整体材料差异，仅供目视检查，不是接口评价。';
      holder.append(element('p', 'difference-note', `${differenceLabel(display)} · ${explanation}${display.difference.status === 'partial' ? ' 部分差异层不可用。' : ''}`));
    } else holder.append(element('p', 'difference-note', '差异图暂不可用；已有叠加图仅展示两侧模型的透明重叠。'));
    return holder;
  }

  function previewWarning(display) {
    return Object.values(display?.metadata?.quality || {}).some(q => q && q.missingFaceCount > 0) ? '预览有缺失曲面，请结合指标判断。' : '';
  }

  function imageLink(url, caption, group, className = '') {
    const safe = resourceURL(url);
    if (!safe) return element('div', 'preview-empty', '此视角暂不可用');
    const anchor = element('a', 'image-link'); anchor.href = safe;
    anchor.dataset.lightbox = group; anchor.dataset.caption = caption; anchor.setAttribute('aria-label', `放大：${caption}`);
    const image = element('img', className); image.src = safe; image.alt = caption; image.loading = 'lazy';
    image.addEventListener('error', () => anchor.replaceWith(element('div', 'preview-empty', '此图片暂不可用')), {once: true});
    anchor.append(image); return anchor;
  }

  function renderGalleries(section, display) {
    const holder = section.querySelector('.multiview-galleries'); holder.replaceChildren();
    for (const kind of ['split', 'overlay']) {
      const title = kind === 'split' ? '四视角对比' : differenceLabel(display, true);
      const gallery = element('section', 'view-gallery'); gallery.dataset.previewKind = kind;
      gallery.append(element('h3', '', title));
      gallery.append(kind === 'split' ? element('p', 'gallery-note', '左侧生成模型，右侧标准答案 · 共用坐标、相机与尺度 · 点击放大') : differenceLegend(display, true));
      const grid = element('div', 'view-grid');
      for (const [direction, label] of Object.entries(angles)) {
        const tile = element('figure'); tile.dataset.angle = direction;
        const url = display?.previews?.[direction]?.[kind] || (kind === 'split' && direction === 'iso' ? display?.thumbnail : null);
        tile.append(imageLink(url, `${rows[active].case} · ${label} · ${kind === 'split' ? '左侧生成模型，右侧标准答案' : title}`, kind));
        tile.append(element('figcaption', '', label)); grid.append(tile);
      }
      gallery.append(grid); holder.append(gallery);
    }
    section.querySelector('.viewer-difference-legend').replaceChildren(differenceLegend(display));
  }

  function setPreview(section, display) {
    const host = section.querySelector('.preview-host'); host.hidden = Boolean(viewer); host.replaceChildren();
    let kind = 'split';
    if (mode === 'difference' && hasStaticDifference(display)) kind = 'overlay';
    if (mode === 'overlay' && !hasStaticDifference(display)) kind = 'overlay';
    const url = display?.previews?.[angle]?.[kind] || (angle === 'iso' && kind === 'split' ? display?.thumbnail : null);
    host.append(imageLink(url, `${rows[active].case} · ${angles[angle]} · ${kind === 'split' ? '左侧生成模型，右侧标准答案' : differenceLabel(display, true)}`, 'viewer', 'static-preview'));
    if (previewWarning(display)) host.append(element('span', 'preview-warning', '预览有缺失曲面'));
  }

  function updateControls(section, display) {
    const staticOnly = !viewer;
    for (const button of section.querySelectorAll('[data-mode]')) {
      const value = button.dataset.mode;
      button.setAttribute('aria-pressed', String(value === mode));
      button.disabled = (value === 'difference' && !(staticOnly ? hasStaticDifference(display) : hasDifference({difference: viewer.difference}))) || (staticOnly && (['candidate', 'reference'].includes(value) || (value === 'overlay' && hasStaticDifference(display))));
      button.title = button.disabled ? (value === 'difference' ? '当前差异图不可用' : '此模式需三维交互；下方可查看四视角图片') : '';
    }
    section.querySelectorAll('[data-view]').forEach(button => button.setAttribute('aria-pressed', String(!customView && button.dataset.view === angle)));
    section.querySelector('.opacity-controls').hidden = mode !== 'overlay' || staticOnly;
    section.querySelector('.comparison-labels').hidden = mode !== 'split';
    section.querySelector('.viewer-difference-legend').hidden = mode !== 'difference';
    section.querySelector('.view-state').textContent = customView ? '自由视角' : angles[angle];
    if (staticOnly) {
      section.querySelector('.viewer-status').textContent = previewWarning(display) + staticReason;
      setPreview(section, display);
    }
  }

  async function loadViewer() {
    destroyViewer();
    if (active < 0) return;
    const current = token, section = sections[active], display = rows[active].display?.views?.[frame];
    const status = section.querySelector('.viewer-status');
    section.querySelectorAll('[data-frame]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.frame === frame)));
    renderGalleries(section, display);
    if (!hasDifference(display) && mode === 'difference') mode = 'split';
    staticReason = online ? '正在加载三维模型…' : '离线静态阅读模式：四个方向按钮可切换预览。运行 serve_report.py 后可旋转模型。';
    updateControls(section, display);
    if (!online) return;
    if (!display || (!display.reference && !display.candidate)) {
      staticReason = '该视图的模型不可用；可切换四视角静态图，评分仍可阅读。'; updateControls(section, display); return;
    }
    abort = new AbortController(); const signal = abort.signal;
    try {
      if (!viewClass) viewClass = (await import(new URL('viewer/comparison-view.js', assetBase).href)).ComparisonView;
      if (current !== token) return;
      const load = async url => {
        if (!url) return null;
        const safe = resourceURL(url); if (!safe) throw Error('Invalid mesh resource');
        const response = await fetch(safe, {signal});
        if (!response.ok) throw Error('Mesh unavailable');
        return response.json();
      };
      const keys = ['reference', 'candidate', 'matched', 'extra', 'missing'];
      const results = await Promise.allSettled(keys.map(key => load(key === 'reference' || key === 'candidate' ? display[key] : hasDifference(display) ? display.difference.layers?.[key] : null)));
      if (current !== token) return;
      const meshes = Object.fromEntries(results.map((result, index) => [keys[index], result.status === 'fulfilled' ? result.value : null]));
      const layerFailures = results.slice(2).some(result => result.status === 'rejected');
      const differenceStatus = layerFailures ? (meshes.matched || meshes.extra || meshes.missing ? 'partial' : 'unavailable') : display.difference?.status;
      const difference = hasDifference(display) ? {...display.difference, status: differenceStatus, layers: {matched: meshes.matched, extra: meshes.extra, missing: meshes.missing}} : null;
      const host = element('div', 'viewer-host'); section.querySelector('.viewer-mount').append(host);
      const instance = new viewClass(host, {onViewChange: state => {
        if (current !== token) return;
        customView = Boolean(state.custom);
        if (!customView && angles[state.view]) angle = state.view;
        const direction = customView ? null : state.view;
        section.querySelector('.view-state').textContent = angles[direction] || '自由视角';
        section.querySelectorAll('[data-view]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.view === direction)));
      }});
      viewer = instance;
      const outcome = await instance.setMeshes({reference: meshes.reference, candidate: meshes.candidate}, difference);
      if (current !== token) return;
      if (outcome?.status === 'failed' || outcome?.status === 'unavailable') throw Error('Viewer preparation failed');
      if (mode === 'difference' && !hasDifference({difference: outcome?.difference})) mode = 'split';
      instance.setMode(mode); instance.setView(angle); applyOpacity(section);
      section.querySelector('.preview-host').hidden = true;
      updateControls(section, display);
      status.textContent = previewWarning(display) + '拖动旋转，滚轮缩放，Shift 拖动平移，双击适配视野。';
      if (!meshes.reference || !meshes.candidate || outcome?.status === 'partial') status.textContent += ' 当前部分模型不可用。';
      if (layerFailures || outcome?.difference?.status === 'partial') status.textContent += ' 部分差异层不可用。';
    } catch (error) {
      if (current !== token || error.name === 'AbortError') return;
      if (viewer) { viewer.dispose(); viewer = null; }
      section.querySelector('.viewer-mount').replaceChildren();
      if (['candidate', 'reference', 'overlay'].includes(mode)) mode = 'split';
      staticReason = '三维模型或 WebGL 不可用；四个方向按钮可切换静态预览。';
      updateControls(section, display);
    }
  }

  function applyOpacity(section) {
    if (!viewer) return;
    const opacity = {};
    section.querySelectorAll('[data-opacity]').forEach(input => { opacity[input.dataset.opacity] = Number(input.value); });
    viewer.setControls({reference: true, candidate: true}, opacity);
  }

  function goTo(index) {
    if (index >= 0 && index < rows.length) location.hash = `fixture=${encodeURIComponent(rows[index].case)}`;
  }

  function detailNavigation() {
    const ordered = [...overview.querySelectorAll('.case-card')];
    const visible = ordered.filter(card => !card.hidden).map(card => Number(card.dataset.index));
    const fallback = !visible.includes(active);
    const indices = fallback ? ordered.map(card => Number(card.dataset.index)) : visible;
    return {indices, position: indices.indexOf(active), fallback};
  }

  function updateDetailNavigation() {
    if (active < 0) return;
    const {indices, position, fallback} = detailNavigation();
    const section = sections[active], label = section.querySelector('.detail-position');
    if (label) label.textContent = `${fallback ? '全部 ' : ''}${position + 1} / ${indices.length}`;
    section.querySelectorAll('[data-step]').forEach(button => {
      const next = position + Number(button.dataset.step);
      button.disabled = position < 0 || next < 0 || next >= indices.length;
    });
  }

  function stepCase(direction) {
    const {indices, position} = detailNavigation();
    const next = position + direction;
    if (position >= 0 && next >= 0 && next < indices.length) goTo(indices[next]);
  }

  function route() {
    closeLightbox();
    const hash = location.hash || '#overview';
    document.querySelectorAll('.page-nav a').forEach(link => {
      if (link.hash === (hash === '#analysis' ? '#analysis' : '#overview')) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    const legacy = /^#case-(\d+)$/.exec(hash);
    let index = legacy ? Number(legacy[1]) : -1;
    if (hash.startsWith('#fixture=')) {
      try { const fixture = decodeURIComponent(hash.slice(9)); index = rows.findIndex(row => String(row.case) === fixture); }
      catch { index = -1; }
    }
    if (index >= rows.length) index = -1;
    document.querySelectorAll('.overview,.case-detail,.appendix').forEach(node => node.classList.remove('active'));
    if (index >= 0) {
      const changed = active !== index;
      active = index;
      if (changed) { frame = !rows[index].display?.views?.scoring?.candidate && rows[index].display?.views?.world?.candidate ? 'world' : 'scoring'; angle = 'iso'; mode = 'split'; customView = false; }
      sections[index].classList.add('active');
      updateDetailNavigation();
      loadViewer();
    } else {
      destroyViewer(); active = -1;
      (hash === '#analysis' ? document.getElementById('analysis') : overview).classList.add('active');
    }
    window.scrollTo({top: 0, behavior: 'instant'});
  }

  function filterCases() {
    const query = document.getElementById('case-search').value.trim().toLocaleLowerCase();
    const state = document.getElementById('case-filter').value;
    const task = document.getElementById('task-filter').value;
    const sort = document.getElementById('case-sort').value;
    let count = 0;
    for (const card of cards) {
      const row = rows[Number(card.dataset.index)];
      const match = (!query || `${row.case} ${row.prompt || ''} ${row.status} ${card.textContent}`.toLocaleLowerCase().includes(query)) && (state === 'all' || state === card.dataset.state) && (task === 'all' || task === card.dataset.task);
      card.hidden = !match; if (match) ++count;
    }
    const ordered = [...cards].sort((a, b) => {
      const x = rows[Number(a.dataset.index)], y = rows[Number(b.dataset.index)];
      if (sort !== 'case') {
        const ax = typeof x.final_score === 'number', by = typeof y.final_score === 'number';
        if (ax !== by) return ax ? -1 : 1;
        if (ax && x.final_score !== y.final_score) return (sort === 'desc' ? -1 : 1) * (x.final_score - y.final_score);
      }
      return String(x.case).localeCompare(String(y.case), 'zh-CN', {numeric: true});
    });
    const grid = document.querySelector('.case-grid'); ordered.forEach(card => grid.append(card));
    document.getElementById('visible-count').textContent = `${count} / ${rows.length} 个案例`;
    document.getElementById('empty-filter').hidden = count !== 0;
    updateDetailNavigation();
  }

  function showInputPage(gallery, index) {
    const figures = [...gallery.querySelectorAll('figure')];
    const page = Math.max(0, Math.min(index, figures.length - 1)); gallery.dataset.page = String(page);
    figures.forEach((figure, i) => { figure.hidden = i !== page; });
    const controls = gallery.querySelector('.gallery-controls');
    if (controls) {
      controls.querySelector('span').textContent = `${page + 1} / ${figures.length}`;
      controls.querySelector('[data-gallery-step="-1"]').disabled = page === 0;
      controls.querySelector('[data-gallery-step="1"]').disabled = page === figures.length - 1;
    }
  }

  function showLightbox(index) {
    lightboxIndex = (index + lightboxItems.length) % lightboxItems.length;
    const item = lightboxItems[lightboxIndex], caption = item.dataset.caption || item.querySelector('img')?.alt || '图片';
    const image = lightbox.querySelector('.lightbox-image'); image.src = item.href; image.alt = caption;
    lightbox.querySelector('.lightbox-caption').textContent = caption;
    lightbox.querySelector('.lightbox-count').textContent = `${lightboxIndex + 1} / ${lightboxItems.length}`;
    lightbox.querySelectorAll('[data-lightbox-step]').forEach(button => { button.disabled = lightboxItems.length < 2; });
  }

  function closeLightbox() {
    if (!lightbox.open) return;
    lightbox.close(); document.body.classList.remove('lightbox-open'); lightboxTrigger?.focus();
  }

  document.querySelectorAll('.input-gallery').forEach(gallery => showInputPage(gallery, 0));
  document.body.classList.add('js');
  document.getElementById('case-search').addEventListener('input', filterCases);
  ['case-filter', 'case-sort', 'task-filter'].forEach(id => document.getElementById(id).addEventListener('change', filterCases));
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-lightbox]');
    if (link && typeof lightbox.showModal === 'function') {
      event.preventDefault(); lightboxTrigger = link;
      lightboxItems = [...link.closest('.case-detail').querySelectorAll('[data-lightbox]')].filter(item => item.dataset.lightbox === link.dataset.lightbox);
      showLightbox(lightboxItems.indexOf(link)); lightbox.showModal(); document.body.classList.add('lightbox-open'); return;
    }
    const button = event.target.closest('button'); if (!button || button.disabled) return;
    if ('lightboxClose' in button.dataset) { closeLightbox(); return; }
    if (button.dataset.lightboxStep) { showLightbox(lightboxIndex + Number(button.dataset.lightboxStep)); return; }
    if (button.dataset.galleryStep) { const gallery = button.closest('.input-gallery'); showInputPage(gallery, Number(gallery.dataset.page || 0) + Number(button.dataset.galleryStep)); return; }
    if (active < 0) return;
    const section = sections[active], display = rows[active].display?.views?.[frame];
    if (button.dataset.step) stepCase(Number(button.dataset.step));
    if (button.dataset.frame) { frame = button.dataset.frame; loadViewer(); }
    if (button.dataset.mode) { mode = button.dataset.mode; viewer?.setMode(mode); updateControls(section, display); }
    if (button.dataset.view) { angle = button.dataset.view; customView = false; viewer?.setView(angle); updateControls(section, display); }
    if ('reset' in button.dataset) { angle = 'iso'; customView = false; viewer?.resetView(); updateControls(section, display); }
  });
  document.addEventListener('input', event => { if (event.target.dataset.opacity && active >= 0) applyOpacity(sections[active]); });
  lightbox.addEventListener('cancel', event => { event.preventDefault(); closeLightbox(); });
  lightbox.addEventListener('click', event => { if (event.target === lightbox) closeLightbox(); });
  document.addEventListener('keydown', event => {
    if (lightbox.open) {
      if (event.key === 'Escape') { event.preventDefault(); closeLightbox(); }
      if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); showLightbox(lightboxIndex + (event.key === 'ArrowRight' ? 1 : -1)); }
      return;
    }
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName) || event.target.isContentEditable || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'Escape') location.hash = 'overview';
    if (active >= 0 && ['j', 'k', 'ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); stepCase(['j', 'ArrowRight'].includes(event.key) ? 1 : -1); }
  });
  window.addEventListener('hashchange', route);
  window.addEventListener('pagehide', destroyViewer);
  filterCases(); route();
  window.addEventListener('load', () => window.scrollTo({top: 0, behavior: 'instant'}), {once: true});
})();
