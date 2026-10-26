/* Static content stays readable without JavaScript, HTTP or WebGL. */
(() => {
  'use strict';
  const assetBase = new URL('.', document.currentScript.src);
  const data = JSON.parse(document.getElementById('report-data').textContent);
  const rows = data.cases || [];
  const sections = [...document.querySelectorAll('.case-detail')];
  const cards = [...document.querySelectorAll('.case-card')];
  const overview = document.getElementById('overview');
  let active = -1, frame = 'scoring', viewer = null, token = 0, abort = null, viewClass = null;

  function destroyViewer() {
    ++token;
    if (abort) abort.abort();
    abort = null;
    if (viewer) { viewer.clear(); viewer.dispose(); viewer = null; }
    document.querySelectorAll('.viewer-mount').forEach(host => host.replaceChildren());
  }

  function resourceURL(value) {
    if (typeof value !== 'string' || !value || value.includes('\\') || value.includes(':') || value.startsWith('/') || value.split('/').includes('..')) throw Error('Invalid report resource');
    return new URL(value, document.baseURI).href;
  }

  function previewWarning(display) {
    return Object.values(display?.metadata?.quality || {}).some(q => q && q.missingFaceCount > 0) ? '预览有缺失曲面，请结合指标判断。' : '';
  }

  function setPreview(section, display) {
    const host = section.querySelector('.preview-host');
    host.hidden = false;
    host.replaceChildren();
    if (display && display.thumbnail) {
      const img = document.createElement('img');
      img.src = resourceURL(display.thumbnail);
      img.className = 'static-preview'; img.alt = '左侧生成模型，右侧标准答案';
      host.append(img);
      if (previewWarning(display)) {
        const note = document.createElement('span'); note.className = 'preview-warning'; note.textContent = '预览有缺失曲面'; host.append(note);
      }
    } else {
      const empty = document.createElement('div'); empty.className = 'preview-empty';
      const label = document.createElement('p'); label.textContent = '该坐标视图无可用预览';
      empty.append(label); host.append(empty);
    }

  }

  async function loadViewer() {
    destroyViewer();
    if (active < 0) return;
    const current = token, section = sections[active], row = rows[active];
    const display = row.display?.views?.[frame];
    const status = section.querySelector('.viewer-status');
    section.querySelectorAll('[data-frame]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.frame === frame)));
    setPreview(section, display);
    if (location.protocol !== 'http:' && location.protocol !== 'https:') {
      status.textContent = previewWarning(display) + '离线静态阅读模式。运行本目录的 serve_report.py 后，可使用三维交互。';
      return;
    }
    if (!display || (!display.reference && !display.candidate)) {
      status.textContent = '该视图的模型不可用；评分与测量证据仍可阅读。'; return;
    }
    status.textContent = '正在加载三维模型…';
    abort = new AbortController(); const signal = abort.signal;
    try {
      if (!viewClass) viewClass = (await import(new URL('viewer/comparison-view.js', assetBase).href)).ComparisonView;
      if (current !== token) return;
      const load = async role => {
        if (!display[role]) return null;
        const response = await fetch(resourceURL(display[role]), {signal});
        if (!response.ok) throw Error('Mesh unavailable');
        return response.json();
      };
      const [reference, candidate] = await Promise.all([load('reference'), load('candidate')]);
      if (current !== token) return;
      const host = document.createElement('div'); host.className = 'viewer-host';
      section.querySelector('.viewer-mount').append(host);
      viewer = new viewClass(host);
      const outcome = await viewer.setMeshes({reference, candidate});
      if (current !== token) return;
      if (outcome?.status === 'failed' || outcome?.status === 'unavailable') throw Error('Viewer preparation failed');
      section.querySelector('.preview-host').hidden = true;
      section.querySelectorAll('[data-mode]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.mode === 'split')));
      viewer.setMode('split');
      section.querySelector('.opacity-controls').hidden = true;
      section.querySelector('.comparison-labels').hidden = false;
      applyOpacity(section);
      const quality = display.metadata?.quality || {};
      const incomplete = Object.values(quality).some(q => q && q.missingFaceCount > 0);
      status.textContent = incomplete ? '预览有缺失曲面，请结合测量证据判断；拖动旋转，滚轮缩放，Shift 拖动平移。' : '拖动旋转，滚轮缩放，Shift 拖动平移，双击适配视野。';
      if (!reference || !candidate) status.textContent += '。当前仅一侧模型可用。';
    } catch (error) {
      if (current !== token || error.name === 'AbortError') return;
      if (viewer) { viewer.dispose(); viewer = null; }
      section.querySelector('.viewer-mount').replaceChildren();
      section.querySelector('.preview-host').hidden = false;
      status.textContent = previewWarning(display) + '三维模型或 WebGL 不可用，已保留静态预览与评测结果。';
    }
  }

  function applyOpacity(section) {
    if (!viewer) return;
    const opacity = {};
    section.querySelectorAll('[data-opacity]').forEach(input => { opacity[input.dataset.opacity] = Number(input.value); });
    viewer.setControls({reference: true, candidate: true}, opacity);
  }

  function route() {
    const hash = location.hash || '#overview';
    document.querySelectorAll('.page-nav a').forEach(link => {
      if (link.hash === (hash === '#analysis' ? '#analysis' : '#overview')) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    const match = /^#case-(\d+)$/.exec(hash);
    let index = match ? Number(match[1]) : -1;
    if (index >= rows.length) index = -1;
    document.querySelectorAll('.overview,.case-detail,.appendix').forEach(node => node.classList.remove('active'));
    if (index >= 0) {
      const changed = active !== index;
      active = index; if (changed) frame = !rows[index].display?.views?.scoring?.candidate && rows[index].display?.views?.world?.candidate ? 'world' : 'scoring';
      sections[index].classList.add('active');
      sections[index].querySelectorAll('[data-step]').forEach(button => {
        const next = active + Number(button.dataset.step); button.disabled = next < 0 || next >= rows.length;
      });
      loadViewer();
    } else {
      destroyViewer(); active = -1;
      const target = hash === '#analysis' ? document.getElementById('analysis') : overview;
      target.classList.add('active');
    }
    window.scrollTo({top: 0, behavior: 'instant'});
  }

  function filterCases() {
    const query = document.getElementById('case-search').value.trim().toLocaleLowerCase();
    const state = document.getElementById('case-filter').value;
    const sort = document.getElementById('case-sort').value;
    let count = 0;
    for (const card of cards) {
      const row = rows[Number(card.dataset.index)];
      const match = (!query || `${row.case} ${row.status} ${card.textContent}`.toLocaleLowerCase().includes(query)) && (state === 'all' || state === card.dataset.state);
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
  }

  function showInputPage(gallery, index) {
    const figures = [...gallery.querySelectorAll('figure')];
    const page = Math.max(0, Math.min(index, figures.length - 1));
    gallery.dataset.page = String(page);
    figures.forEach((figure, i) => { figure.hidden = i !== page; });
    const controls = gallery.querySelector('.gallery-controls');
    if (controls) {
      controls.querySelector('span').textContent = `${page + 1} / ${figures.length}`;
      controls.querySelector('[data-gallery-step="-1"]').disabled = page === 0;
      controls.querySelector('[data-gallery-step="1"]').disabled = page === figures.length - 1;
    }
  }
  document.querySelectorAll('.input-gallery').forEach(gallery => showInputPage(gallery, 0));
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-gallery-step]');
    if (!button) return;
    const gallery = button.closest('.input-gallery');
    showInputPage(gallery, Number(gallery.dataset.page || 0) + Number(button.dataset.galleryStep));
  });

  document.body.classList.add('js');
  document.getElementById('case-search').addEventListener('input', filterCases);
  document.getElementById('case-filter').addEventListener('change', filterCases);
  document.getElementById('case-sort').addEventListener('change', filterCases);
  document.addEventListener('click', event => {
    const button = event.target.closest('button'); if (!button || active < 0) return;
    const section = sections[active];
    if (button.dataset.step) { const next = active + Number(button.dataset.step); if (next >= 0 && next < rows.length) location.hash = `case-${next}`; }
    if (button.dataset.frame) { frame = button.dataset.frame; loadViewer(); }
    if (button.dataset.mode && viewer) {
      viewer.setMode(button.dataset.mode);
      section.querySelector('.opacity-controls').hidden = button.dataset.mode !== 'overlay';
      section.querySelector('.comparison-labels').hidden = button.dataset.mode !== 'split';
      section.querySelectorAll('[data-mode]').forEach(other => other.setAttribute('aria-pressed', String(other === button)));
    }
    if (button.dataset.view && viewer) viewer.setView(button.dataset.view);
  });
  document.addEventListener('input', event => { if (event.target.dataset.opacity && active >= 0) applyOpacity(sections[active]); });
  document.addEventListener('keydown', event => {
    if (['INPUT','SELECT','TEXTAREA'].includes(event.target.tagName)) return;
    if (event.key === 'Escape') location.hash = 'overview';
    if (active >= 0 && ['j','k'].includes(event.key)) { const next = active + (event.key === 'j' ? 1 : -1); if (next >= 0 && next < rows.length) location.hash = `case-${next}`; }
  });
  window.addEventListener('hashchange', route);
  window.addEventListener('pagehide', destroyViewer);
  filterCases(); route();
  window.addEventListener('load', () => window.scrollTo({top: 0, behavior: 'instant'}), {once: true});
})();
