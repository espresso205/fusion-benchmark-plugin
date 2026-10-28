/* Static content stays readable without JavaScript, HTTP or WebGL. */
(() => {
  'use strict';
  const assetBase = new URL('.', document.currentScript.src);
  const data = JSON.parse(document.getElementById('report-data').textContent);
  const rows = data.cases || [];
  const sections = [...document.querySelectorAll('.case-detail')];
  const cards = [...document.querySelectorAll('.case-card')];
  const overview = document.getElementById('overview');
  const angles = {iso: '等轴', front: '前视', top: '俯视', right: '右视'};
  const online = ['http:', 'https:'].includes(location.protocol);
  let active = -1, frame = 'scoring', mode = 'split', angle = 'iso', customView = false;
  let viewer = null, token = 0, abort = null, viewClass = null, staticReason = '';
  let lightboxItems = [], lightboxIndex = 0, lightboxTrigger = null;
  let differenceLayers = {matched: true, extra: true, missing: true};
  let galleryKind = 'split';
  const lightbox = document.getElementById('image-lightbox');
  const lightboxImage = lightbox.querySelector('.lightbox-image');
  const imageView = {scale: 1, x: 0, y: 0, fitting: true, dragging: null};
  let lightboxViewport, zoomLabel;

  function resetComparison(section) {
    frame = 'scoring'; mode = 'split'; angle = 'iso'; customView = false;
    differenceLayers = {matched: true, extra: true, missing: true};
    galleryKind = 'split';
    section.querySelectorAll('[data-opacity]').forEach(input => {
      input.value = input.dataset.opacity === 'candidate' ? '0.65' : '0.35';
    });
    section.querySelectorAll('details').forEach(details => { details.open = false; });
    section.querySelectorAll('.input-gallery').forEach(gallery => showInputPage(gallery, 0));
    closeLightbox();
  }

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
    return hasDifference(display) && Object.keys(angles).some(direction => previewURL(display, direction, 'difference'));
  }

  function previewURL(display, direction, kind) {
    const preview = display?.previews?.[direction];
    const metadata = display?.metadata?.preview;
    const legacyDifference = hasDifference(display) && !preview?.difference &&
      metadata?.overlay_kind !== 'ordinary_overlay' && metadata?.overlay_fallback !== 'ordinary_overlay';
    if (kind === 'difference') return preview?.difference || (legacyDifference ? preview?.overlay : null);
    if (kind === 'overlay' && legacyDifference) return null;
    return preview?.[kind] || (kind === 'split' && direction === 'iso' ? display?.thumbnail : null);
  }

  function differenceLabel(display, staticImage = false) {
    if ((staticImage && !hasStaticDifference(display)) || !hasDifference(display)) return '差异着色';
    return display.difference.scope === 'interface' ? 'Interface 区域差异' : '全模型差异';
  }

  function differenceLegend(display, staticImage = false, raw = false) {
    const holder = element('div');
    if (staticImage && hasDifference(display) && !hasStaticDifference(display)) {
      holder.append(element('p', 'difference-note', '此坐标的差异图片不可用，可查看透明叠加或三维差异。')); return holder;
    }
    if (hasDifference(display)) {
      const legend = element('div', 'difference-legend');
      const matchedLabel = !raw && display.difference.visualization?.kind === 'surface-distance-tolerance' ? '重合或容差内' : '重合';
      for (const [key, label] of Object.entries({matched: matchedLabel, extra: '多余', missing: '缺失'})) legend.append(element('span', key, label));
      holder.append(legend);
      if (!staticImage && viewer) {
        const controls = element('div', 'difference-layer-controls');
        controls.setAttribute('aria-label', '选择显示差异区域');
        for (const [key, label] of Object.entries({matched: '显示蓝色区域', extra: '显示多余', missing: '显示缺失'})) {
          const item = element('label'), input = document.createElement('input');
          input.type = 'checkbox'; input.dataset.differenceLayer = key; input.checked = differenceLayers[key];
          input.addEventListener('change', () => {
            differenceLayers[key] = input.checked; viewer?.setDifferenceLayers(differenceLayers);
          });
          item.append(input, document.createTextNode(label)); controls.append(item);
        }
        holder.append(controls);
      }
      const explanation = raw ? '原始三角网格的布尔体积分区，不使用显示容差。细薄区域可能受到网格近似影响，仅供目视检查。' :
        display.difference.visualization?.description || (display.difference.scope === 'interface' ? '依据提供的 KOR / KIR 区域显示，不代表 Interface 评分。' : '三角网格的整体材料差异，仅供目视检查，不是接口评价。');
      holder.append(element('p', 'difference-note', `${explanation}${display.difference.status === 'partial' ? ' 部分差异层不可用。' : ''}`));
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
    const tabs = element('div', 'gallery-mode-tabs'); tabs.setAttribute('aria-label', '四视图显示方式');
    const select = () => {
      holder.querySelectorAll('.view-gallery').forEach(node => {node.hidden = node.dataset.previewKind !== galleryKind;});
      tabs.querySelectorAll('button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.galleryKind === galleryKind)));
    };
    for (const [kind, label] of Object.entries({split: '并排四视图', overlay: '透明叠加四视图', difference: '差异四视图'})) {
      const button = element('button', '', label); button.type = 'button'; button.dataset.galleryKind = kind;
      button.addEventListener('click', () => {galleryKind = kind; select();}); tabs.append(button);
    }
    holder.append(tabs);
    for (const kind of ['split', 'overlay', 'difference']) {
      const title = {split: '四视角对比', overlay: '透明叠加', difference: differenceLabel(display, true)}[kind];
      const gallery = element('section', 'view-gallery'); gallery.dataset.previewKind = kind;
      gallery.append(element('h3', '', title));
      gallery.append(kind === 'difference' ? differenceLegend(display, true) : element('p', 'gallery-note', kind === 'split' ? '左侧生成模型，右侧标准答案。共用坐标、相机与尺度，点击放大。' : '两侧模型在同一视角透明叠加，点击放大。'));
      const grid = element('div', 'view-grid');
      for (const [direction, label] of Object.entries(angles)) {
        const tile = element('figure'); tile.dataset.angle = direction;
        const url = previewURL(display, direction, kind);
        tile.append(imageLink(url, `${rows[active].case} ${label} ${kind === 'split' ? '左侧生成模型，右侧标准答案' : title}`, kind));
        tile.append(element('figcaption', '', label)); grid.append(tile);
      }
      gallery.append(grid); holder.append(gallery);
    }
    select();
    section.querySelector('.viewer-difference-legend').replaceChildren(differenceLegend(display));
  }

  function setPreview(section, display) {
    const host = section.querySelector('.preview-host'); host.hidden = Boolean(viewer); host.replaceChildren();
    const kind = ['overlay', 'difference'].includes(mode) ? mode : 'split';
    const url = previewURL(display, angle, kind);
    const label = {split: '左侧生成模型，右侧标准答案', overlay: '透明叠加', difference: differenceLabel(display, true)}[kind];
    host.append(imageLink(url, `${rows[active].case} ${angles[angle]} ${label}`, 'viewer', 'static-preview'));
    if (previewWarning(display)) host.append(element('span', 'preview-warning', '预览有缺失曲面'));
  }

  function updateControls(section, display) {
    const staticOnly = !viewer;
    for (const button of section.querySelectorAll('[data-mode]')) {
      const value = button.dataset.mode;
      button.setAttribute('aria-pressed', String(value === mode));
      button.disabled = (['difference', 'difference_raw'].includes(value) && !(staticOnly ? value === 'difference' && hasStaticDifference(display) : hasDifference({difference: viewer.difference}))) || (staticOnly && (['candidate', 'reference'].includes(value) || (value === 'overlay' && !previewURL(display, angle, 'overlay'))));
      button.title = button.disabled ? (value === 'difference' ? '当前差异图不可用' : '此模式需三维交互；下方可查看四视角图片') : '';
    }
    section.querySelectorAll('[data-view]').forEach(button => button.setAttribute('aria-pressed', String(!customView && button.dataset.view === angle)));
    section.querySelector('.opacity-controls').hidden = mode !== 'overlay' || staticOnly;
    section.querySelector('.comparison-labels').hidden = mode !== 'split';
    const legend = section.querySelector('.viewer-difference-legend');
    legend.hidden = !['difference', 'difference_raw'].includes(mode);
    if (!legend.hidden) legend.replaceChildren(differenceLegend(viewer ? {...display, difference: viewer.difference} : display, staticOnly, mode === 'difference_raw'));
    section.querySelector('.view-state').textContent = customView ? '自由视角' : angles[angle];
    if (staticOnly) {
      section.querySelector('.viewer-status').textContent = previewWarning(display) + staticReason;
      setPreview(section, display);
    }
  }

  async function loadViewer({cameraState = null} = {}) {
    destroyViewer();
    if (active < 0) return;
    const current = token, section = sections[active], display = rows[active].display?.views?.[frame];
    const status = section.querySelector('.viewer-status');
    section.querySelectorAll('[data-frame]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.frame === frame)));
    renderGalleries(section, display);
    if (!hasDifference(display) && ['difference', 'difference_raw'].includes(mode)) mode = 'split';
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
      const keys = ['reference', 'candidate', 'matched', 'extra', 'missing', 'surface_matched', 'surface_extra', 'surface_missing'];
      const results = await Promise.allSettled(keys.map(key => load(key === 'reference' || key === 'candidate' ? display[key] :
        hasDifference(display) ? key.startsWith('surface_') ? display.difference.surface_layers?.[key.slice(8)] : display.difference.layers?.[key] : null)));
      if (current !== token) return;
      const meshes = Object.fromEntries(results.map((result, index) => [keys[index], result.status === 'fulfilled' ? result.value : null]));
      const layerFailures = results.slice(2, 5).some(result => result.status === 'rejected');
      const surfaceFailures = results.slice(5).some(result => result.status === 'rejected');
      const differenceStatus = layerFailures ? (meshes.matched || meshes.extra || meshes.missing ? 'partial' : 'unavailable') : display.difference?.status;
      const surfaceLayers = !surfaceFailures && display.difference?.surface_layers ?
        {matched: meshes.surface_matched, extra: meshes.surface_extra, missing: meshes.surface_missing} : null;
      const difference = hasDifference(display) ? {...display.difference, status: differenceStatus,
        layers: {matched: meshes.matched, extra: meshes.extra, missing: meshes.missing}, surface_layers: surfaceLayers,
        ...(surfaceFailures ? {visualization: {kind: 'raw-volume', tolerance_mm: 0, description: '表面差异层加载失败，当前显示原始体积差异，不使用显示容差。'}} : {})} : null;
      const host = element('div', 'viewer-host'); section.querySelector('.viewer-mount').append(host);
      // Constructor and mesh preparation notify their own ISO defaults. Only
      // accept callbacks after applying the user's requested comparison state.
      let initialized = false;
      const instance = new viewClass(host, {onViewChange: state => {
        if (current !== token || !initialized) return;
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
      if (['difference', 'difference_raw'].includes(mode) && !hasDifference({difference: outcome?.difference})) mode = 'split';
      instance.setMode(mode); instance.setView(angle); instance.setDifferenceLayers(differenceLayers);
      if (cameraState && instance.setCameraState) instance.setCameraState(cameraState);
      else customView = false;
      applyOpacity(section); initialized = true;
      section.querySelector('.preview-host').hidden = true;
      updateControls(section, display);
      status.textContent = previewWarning(display) + '拖动旋转，滚轮缩放，Shift 拖动平移，双击适配视野。';
      if (!meshes.reference || !meshes.candidate || outcome?.status === 'partial') status.textContent += ' 当前部分模型不可用。';
      if (layerFailures || outcome?.difference?.status === 'partial') status.textContent += ' 部分差异层不可用。';
      if (surfaceFailures) status.textContent += ' 表面差异层不可用，已回退原始体积差异。';
    } catch (error) {
      if (current !== token || error.name === 'AbortError') return;
      if (viewer) { viewer.dispose(); viewer = null; }
      section.querySelector('.viewer-mount').replaceChildren();
      if (['candidate', 'reference', 'difference_raw'].includes(mode) || (mode === 'overlay' && !previewURL(display, angle, 'overlay'))) mode = 'split';
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
      if (changed) resetComparison(sections[index]);
      sections[index].classList.add('active');
      updateDetailNavigation();
      if (changed || !viewer) loadViewer();
      const canonical = `#fixture=${encodeURIComponent(rows[index].case)}`;
      if (location.hash !== canonical) history.replaceState(null, '', canonical);
    } else {
      destroyViewer(); active = -1;
      (hash === '#analysis' ? document.getElementById('analysis') : overview).classList.add('active');
      if (!['#overview', '#analysis'].includes(hash)) history.replaceState(null, '', '#overview');
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
      const match = (!query || `${row.case} ${row.enterprise_id || ''} ${row.enterprise_title || row.title || ''} ${row.prompt || ''} ${row.status} ${card.textContent}`.toLocaleLowerCase().includes(query)) && (state === 'all' || state === card.dataset.state) && (task === 'all' || task === card.dataset.task);
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

  function showLightbox(index, preserveInspection = false) {
    if (!lightboxItems.length) return;
    lightboxIndex = (index + lightboxItems.length) % lightboxItems.length;
    const item = lightboxItems[lightboxIndex], caption = item.dataset.caption || item.querySelector('img')?.alt || '图片';
    if (!preserveInspection) {imageView.fitting = true; imageView.x = 0; imageView.y = 0;}
    lightboxImage.src = item.href; lightboxImage.alt = caption;
    if (lightboxImage.complete && lightboxImage.naturalWidth) imageView.fitting ? fitLightbox() : paintLightbox();
    lightbox.querySelector('.lightbox-caption').textContent = caption;
    lightbox.querySelector('.lightbox-count').textContent = `${lightboxIndex + 1} / ${lightboxItems.length}`;
    lightbox.querySelectorAll('[data-lightbox-step]').forEach(button => { button.disabled = lightboxItems.length < 2; });
    const modeControls = lightbox.querySelector('.lightbox-mode-controls');
    modeControls.hidden = !['split', 'overlay', 'difference'].includes(item.dataset.lightbox);
    modeControls.querySelectorAll('button').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.lightboxMode === item.dataset.lightbox));
      const direction = item.closest('figure')?.dataset.angle;
      button.disabled = !sections[active]?.querySelector(`[data-preview-kind="${button.dataset.lightboxMode}"] figure[data-angle="${direction}"] a[data-lightbox]`);
    });
  }

  function closeLightbox() {
    if (!lightbox.open) return;
    imageView.dragging = null;
    lightbox.close(); document.body.classList.remove('lightbox-open'); lightboxTrigger?.focus();
  }

  function paintLightbox() {
    if (!lightboxImage.naturalWidth) return;
    lightboxImage.style.width = `${lightboxImage.naturalWidth}px`;
    lightboxImage.style.height = `${lightboxImage.naturalHeight}px`;
    lightboxImage.style.transform = `translate(-50%, -50%) translate(${imageView.x}px, ${imageView.y}px) scale(${imageView.scale})`;
    zoomLabel.textContent = `${Math.round(imageView.scale * 100)}%`;
  }

  function fitLightbox() {
    if (!lightbox.open || !lightboxImage.naturalWidth) return;
    imageView.scale = Math.min(1, (lightboxViewport.clientWidth - 24) / lightboxImage.naturalWidth,
      (lightboxViewport.clientHeight - 24) / lightboxImage.naturalHeight);
    imageView.scale = Math.max(.01, imageView.scale);
    imageView.x = 0; imageView.y = 0; imageView.fitting = true;
    paintLightbox();
  }

  function zoomLightbox(value, position = null) {
    if (!lightboxImage.naturalWidth) return;
    const scale = Math.max(.01, Math.min(8, value));
    const ratio = scale / imageView.scale;
    const x = position?.x || 0, y = position?.y || 0;
    imageView.x = x - (x - imageView.x) * ratio;
    imageView.y = y - (y - imageView.y) * ratio;
    imageView.scale = scale; imageView.fitting = false;
    paintLightbox();
  }

  function setupLightbox() {
    lightboxViewport = element('div', 'lightbox-viewport');
    lightboxViewport.tabIndex = 0;
    lightboxViewport.setAttribute('aria-label', '图片查看区：滚轮缩放，拖动平移');
    lightboxImage.replaceWith(lightboxViewport); lightboxViewport.append(lightboxImage);
    lightboxImage.draggable = false;
    const controls = element('div', 'lightbox-zoom-controls');
    controls.setAttribute('role', 'group'); controls.setAttribute('aria-label', '图片缩放');
    for (const [action, label] of [['out', '缩小'], ['in', '放大'], ['fit', '适应窗口'], ['actual', '1:1']]) {
      const button = element('button', '', label); button.type = 'button'; button.dataset.lightboxZoom = action;
      controls.append(button);
    }
    zoomLabel = element('span', 'lightbox-zoom-level', '100%'); zoomLabel.setAttribute('aria-live', 'polite');
    controls.append(zoomLabel); lightbox.querySelector('.lightbox-controls').prepend(controls);
    const modes = element('div', 'lightbox-mode-controls');
    for (const [kind, label] of Object.entries({split: '并排', overlay: '透明叠加', difference: '差异'})) {
      const button = element('button', '', label); button.type = 'button'; button.dataset.lightboxMode = kind;
      button.addEventListener('click', () => {
        const direction = lightboxItems[lightboxIndex]?.closest('figure')?.dataset.angle;
        const links = [...sections[active].querySelectorAll(`[data-preview-kind="${kind}"] a[data-lightbox]`)];
        const index = links.findIndex(link => link.closest('figure').dataset.angle === direction);
        if (index >= 0) {lightboxItems = links; showLightbox(index, true);}
      }); modes.append(button);
    }
    lightbox.querySelector('.lightbox-toolbar').insertBefore(modes, lightbox.querySelector('[data-lightbox-close]'));
    lightboxImage.addEventListener('load', () => imageView.fitting ? fitLightbox() : paintLightbox());
    lightboxImage.addEventListener('error', () => { zoomLabel.textContent = '图片不可用'; });
    lightboxViewport.addEventListener('wheel', event => {
      event.preventDefault();
      const bounds = lightboxViewport.getBoundingClientRect();
      zoomLightbox(imageView.scale * (event.deltaY < 0 ? 1.2 : 1 / 1.2),
        {x: event.clientX - bounds.left - bounds.width / 2, y: event.clientY - bounds.top - bounds.height / 2});
    }, {passive: false});
    lightboxViewport.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault(); imageView.dragging = {x: event.clientX, y: event.clientY};
      lightboxViewport.setPointerCapture(event.pointerId); lightboxViewport.classList.add('dragging');
    });
    lightboxViewport.addEventListener('pointermove', event => {
      if (!imageView.dragging) return;
      imageView.x += event.clientX - imageView.dragging.x; imageView.y += event.clientY - imageView.dragging.y;
      imageView.dragging = {x: event.clientX, y: event.clientY}; imageView.fitting = false; paintLightbox();
    });
    for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) lightboxViewport.addEventListener(type, () => {
      imageView.dragging = null; lightboxViewport.classList.remove('dragging');
    });
    lightboxViewport.addEventListener('dblclick', fitLightbox);
    window.addEventListener('resize', () => { if (imageView.fitting) fitLightbox(); });
  }

  setupLightbox();
  document.querySelectorAll('.input-gallery').forEach(gallery => showInputPage(gallery, 0));
  document.body.classList.add('js');
  document.getElementById('case-search').addEventListener('input', filterCases);
  ['case-filter', 'case-sort', 'task-filter'].forEach(id => document.getElementById(id).addEventListener('change', filterCases));
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-lightbox]');
    if (link && typeof lightbox.showModal === 'function') {
      event.preventDefault(); lightboxTrigger = link;
      lightboxItems = [...link.closest('.case-detail').querySelectorAll('[data-lightbox]')].filter(item => item.dataset.lightbox === link.dataset.lightbox);
      lightbox.showModal(); document.body.classList.add('lightbox-open'); showLightbox(lightboxItems.indexOf(link)); return;
    }
    const button = event.target.closest('button'); if (!button || button.disabled) return;
    if ('lightboxClose' in button.dataset) { closeLightbox(); return; }
    if (button.dataset.lightboxZoom) {
      const action = button.dataset.lightboxZoom;
      if (action === 'fit') fitLightbox();
      else if (action === 'actual') { imageView.x = 0; imageView.y = 0; zoomLightbox(1); }
      else zoomLightbox(imageView.scale * (action === 'in' ? 1.25 : 1 / 1.25));
      return;
    }
    if (button.dataset.lightboxStep) { showLightbox(lightboxIndex + Number(button.dataset.lightboxStep)); return; }
    if (button.dataset.galleryStep) { const gallery = button.closest('.input-gallery'); showInputPage(gallery, Number(gallery.dataset.page || 0) + Number(button.dataset.galleryStep)); return; }
    if (active < 0) return;
    const section = sections[active], display = rows[active].display?.views?.[frame];
    if (button.dataset.step) stepCase(Number(button.dataset.step));
    if (button.dataset.frame && button.dataset.frame !== frame) {
      const cameraState = viewer?.getCameraState?.();
      frame = button.dataset.frame; loadViewer({cameraState});
    }
    if (button.dataset.mode) { mode = button.dataset.mode; viewer?.setMode(mode); updateControls(section, display); }
    if (button.dataset.view) { angle = button.dataset.view; customView = false; viewer?.setView(angle); updateControls(section, display); }
    if ('reset' in button.dataset) { resetComparison(section); loadViewer(); }
  });
  document.addEventListener('input', event => { if (event.target.dataset.opacity && active >= 0) applyOpacity(sections[active]); });
  lightbox.addEventListener('cancel', event => { event.preventDefault(); closeLightbox(); });
  lightbox.addEventListener('click', event => { if (event.target === lightbox) closeLightbox(); });
  document.addEventListener('keydown', event => {
    if (lightbox.open) {
      if (event.key === 'Escape') { event.preventDefault(); closeLightbox(); }
      if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); showLightbox(lightboxIndex + (event.key === 'ArrowRight' ? 1 : -1)); }
      if (['+', '=', '-', '0', '1'].includes(event.key)) {
        event.preventDefault();
        if (event.key === '0') fitLightbox();
        else if (event.key === '1') { imageView.x = 0; imageView.y = 0; zoomLightbox(1); }
        else zoomLightbox(imageView.scale * (event.key === '-' ? 1 / 1.25 : 1.25));
      }
      return;
    }
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName) || event.target.isContentEditable || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'Escape') location.hash = 'overview';
    if (active >= 0 && ['j', 'k', 'ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); stepCase(['j', 'ArrowRight'].includes(event.key) ? 1 : -1); }
  });
  window.addEventListener('hashchange', route);
  window.addEventListener('pagehide', destroyViewer);
  window.addEventListener('pageshow', event => { if (event.persisted) { active = -1; route(); } });
  filterCases(); route();
  window.addEventListener('load', () => window.scrollTo({top: 0, behavior: 'instant'}), {once: true});
})();
