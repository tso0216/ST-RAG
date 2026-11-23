// 聊天加地圖。地圖只畫伺服器算好的場景（scene），這裡不判斷要畫什麼。
const $ = (selector) => document.querySelector(selector);
const esc = (text) => String(text ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const ACCENT = '#2f5fd0';
const DIM = 0.14;
const PREVIEW_LINES = 3;      // tool 結果先顯示幾行
const LABEL_LIMIT = 10;       // 點比這多時，只有查詢目標標名稱
const HOME = [36.4, 137.8];
const HOME_ZOOM = 6;
const REDUCED_MOTION = matchMedia('(prefers-reduced-motion: reduce)').matches;

const state = { active: null, follow: true, scene: null, focus: null, filter: null, panel: false, controller: null, multi: false };
let drawn = { dots: [], pins: [], lines: [], labels: [] };
const pending = el('div', 'pending');
class StaleServer extends Error {}   // 前端已更新、伺服器還沒重開時，事件的欄位對不上

// ---------- 地圖 ----------
const map = L.map('map', {
  zoomControl: false, minZoom: 5, maxZoom: 18, scrollWheelZoom: REDUCED_MOTION,
  zoomAnimation: !REDUCED_MOTION, fadeAnimation: !REDUCED_MOTION, markerZoomAnimation: !REDUCED_MOTION,
}).setView(HOME, HOME_ZOOM);
map.attributionControl.setPrefix(false);
const TILES = 'https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png';
// 底下墊一層低解析度的日本全圖：飛行途中新的圖磚還沒載入，才不會露出空白
L.tileLayer(TILES, { minZoom: 5, maxZoom: 18, minNativeZoom: 7, maxNativeZoom: 7, keepBuffer: 24, zIndex: 0 }).addTo(map);
L.tileLayer(TILES, {
  minZoom: 5, maxZoom: 18, updateWhenZooming: false, keepBuffer: 4, zIndex: 1,
  attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html">地理院タイル</a>',
}).addTo(map);
for (const [name, z] of [['scene', 420], ['pins', 620], ['labels', 640]]) map.createPane(name).style.zIndex = z;
map.getPane('labels').style.pointerEvents = 'none';
const sceneLayer = L.layerGroup().addTo(map);
// 範圍、線、點畫在同一張 canvas：點多時比 SVG 順，滑鼠事件也不會被上層擋住
const canvas = L.canvas({ pane: 'scene', tolerance: 6, padding: 0.4 });

map.on('click', () => {
  if (state.filter || state.focus) { state.filter = null; state.focus = null; applyFocus(); renderEvidence(); }
  else if (state.panel) { state.panel = false; renderEvidence(); }
});
map.on('zoomend moveend', placeLabels);

const LINE_STYLE = {
  route: { color: ACCENT, weight: 2, opacity: 0.5 },
  move: { color: ACCENT, weight: 2, opacity: 0.5 },
  leg: { color: ACCENT, weight: 1.5, opacity: 0.7, dashArray: '4 5' },
};

function addLabel(latlng, text, layer, priority, isTag = false) {
  const icon = L.divIcon({ className: '', iconSize: [0, 0], html: `<span class="map-label${isTag ? ' is-tag' : ''}">${esc(text)}</span>` });
  const marker = L.marker(latlng, { pane: 'labels', icon, interactive: false, keyboard: false }).addTo(sceneLayer);
  drawn.labels.push({ latlng, text, layer, priority, isTag, node: marker.getElement().firstChild, off: false });
}

// 名稱互相蓋到時，只留比較重要的；被藏起來的滑過去才顯示
function placeLabels() {
  const taken = [];
  for (const item of [...drawn.labels].sort((a, b) => b.priority - a.priority)) {
    item.width ??= item.node.offsetWidth;
    const at = map.latLngToContainerPoint(item.latlng);
    const box = { l: at.x - item.width / 2 - 3, r: at.x + item.width / 2 + 3, t: at.y - (item.isTag ? 10 : 24), b: at.y + (item.isTag ? 10 : -6) };
    const off = taken.some((o) => box.l < o.r && box.r > o.l && box.t < o.b && box.b > o.t);
    if (!off) taken.push(box);
    if (off !== item.off) {
      item.off = off;
      item.node.classList.toggle('off', off);
      if (item.layer) {
        item.layer.unbindTooltip();
        if (off) item.layer.bindTooltip(esc(item.text), { direction: 'top', className: 'tip', offset: [0, -6] });
      }
    }
  }
}

function drawLine(line) {
  const style = LINE_STYLE[line.kind];
  const path = L.polyline(line.coords, { ...style, renderer: canvas }).addTo(sceneLayer);
  const item = { line, path, style };
  path.bindTooltip(esc(line.title), { sticky: true, className: 'tip' });
  path.on('mouseover', () => path.setStyle({ weight: style.weight + 1.5, opacity: 1 }));
  path.on('mouseout', () => path.setStyle(lineLook(item)));
  if (line.refs.length) path.on('click', (e) => { L.DomEvent.stop(e); setFilter(line.refs); });
  drawn.lines.push(item);
  if (line.tag) {
    const [a, b] = line.coords;
    addLabel([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], line.tag, null, 1, true);
  }
}

function drawPoint(pt, labelled) {
  const pick = (e) => { L.DomEvent.stop(e); if (pt.refs.length) setFilter(pt.refs); };
  let layer;
  if (pt.seq.length) {
    const text = pt.seq.join('·');
    const width = Math.max(20, text.length * 7 + 8);
    layer = L.marker([pt.lat, pt.lon], {
      pane: 'pins', alt: pt.name, riseOnHover: true,
      icon: L.divIcon({ className: '', iconSize: [width, 20], html: `<div class="pin">${esc(text)}</div>` }),
    }).addTo(sceneLayer);
    drawn.pins.push({ pt, layer });
  } else {
    layer = L.circleMarker([pt.lat, pt.lon], {
      renderer: canvas, radius: pt.main ? 6 : 4, weight: pt.main ? 2 : 1.5, color: '#fff', opacity: 1, fillColor: ACCENT, fillOpacity: 1,
    }).addTo(sceneLayer);
    drawn.dots.push({ pt, layer });
  }
  layer.on('click', pick);
  if (labelled) addLabel([pt.lat, pt.lon], pt.name, layer, pt.main ? 3 : 2);
  else layer.bindTooltip(esc(pt.name), { direction: 'top', className: 'tip', offset: [0, -6] });
}

function hasDrawing(scene) {
  return Boolean(scene && (scene.points.length || scene.areas.length || scene.circles.length || scene.lines.length));
}

function render(scene) {
  sceneLayer.clearLayers();
  drawn = { dots: [], pins: [], lines: [], labels: [] };
  state.scene = scene;
  state.focus = null;
  state.filter = null;
  clearCites();
  renderEvidence();
  if (!scene) { stopMoving(); showScene(false); return; }

  const zone = { renderer: canvas, interactive: false, color: ACCENT, weight: 1, opacity: 0.45, fillColor: ACCENT, fillOpacity: 0.05 };
  const bounds = L.latLngBounds([]);
  for (const area of scene.areas) {
    const layer = L.geoJSON({ type: 'MultiPolygon', coordinates: area.coordinates }, { ...zone, style: () => zone }).addTo(sceneLayer);
    bounds.extend(layer.getBounds());
  }
  for (const c of scene.circles) {
    const circle = L.circle([c.lat, c.lon], { ...zone, radius: c.km * 1000 }).addTo(sceneLayer);
    bounds.extend(circle.getBounds());
  }
  scene.lines.forEach(drawLine);
  const few = scene.points.length <= LABEL_LIMIT;
  // 小的先畫，查詢目標蓋在上面
  for (const p of [...scene.points].sort((a, b) => a.main - b.main)) {
    drawPoint(p, few || p.main);
    bounds.extend([p.lat, p.lon]);
  }
  placeLabels();
  if (bounds.isValid()) moveTo(bounds, { reveal: true });
  else showScene();
}

const SCENE_PANES = ['scene', 'pins', 'labels'];
let sceneHidden = false;

function hideScene() {
  sceneHidden = true;
  for (const name of SCENE_PANES) {
    const pane = map.getPane(name);
    pane.getAnimations().forEach((a) => a.cancel());
    pane.style.opacity = 0;
  }
}

function showScene(fade = true) {
  sceneHidden = false;
  for (const name of SCENE_PANES) {
    const pane = map.getPane(name);
    pane.getAnimations().forEach((a) => a.cancel());
    pane.style.opacity = '';
    if (!fade || REDUCED_MOTION) continue;
    const anim = pane.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 220, easing: 'ease-out' });
    setTimeout(() => anim.finish(), 300);   // 分頁沒在繪製時動畫不會前進，不能停在透明
  }
}

function viewFor(bounds, maxZoom) {
  map.invalidateSize({ pan: false, animate: false });
  const size = map.getSize();
  if (!size.x || !size.y) return null;     // 地圖沒有尺寸時 flyTo 會算出 NaN，之後每一幀都報錯
  const inset = Math.floor(Math.min(48, size.x / 4, size.y / 4));
  const evidence = $('#evidence');
  const panel = evidence.hidden ? 0 : evidence.offsetWidth + 12;
  const side = panel ? Math.min(inset, 24) : inset;     // 面板開著時左右留白小一點，點才放得下
  const right = side + (size.x - side * 2 - panel >= 100 ? panel : 0);    // 太窄放不下面板時就不避開
  const options = { paddingTopLeft: [side, inset], paddingBottomRight: [right, inset], maxZoom };
  const zoom = map.getZoom();
  const target = Math.min(maxZoom, map.getBoundsZoom(bounds, false, L.point(side + right, inset * 2)));
  const visible = L.latLngBounds(map.containerPointToLatLng([24, size.y - 24]), map.containerPointToLatLng([size.x - right + 24, 24]));
  const settled = visible.contains(bounds) && target - zoom <= 1 && target >= zoom;
  const far = !settled && (Math.abs(target - zoom) >= 2 || !map.getBounds().pad(0.4).contains(bounds));
  return { options, settled, far };
}

let moveTimer = null;
let flight = null;      // 飛行中時是取消這趟的函式

// map.stop() 會把縮放拉回整數級，畫面會跳一下；這裡只停動畫
function halt() {
  L.Util.cancelAnimFrame(map._flyToFrame);
  map._panAnim?.stop();
}

function stopMoving() {
  clearTimeout(moveTimer);
  endWheel();
  const flying = Boolean(flight);
  flight?.();
  halt();
  if (flying) map._moveEnd(true);     // 飛到一半停下來也要通知圖磚和標籤更新
}

// 工具結果會連續到，等一下只移動最後一次；已經看得到就不動，地圖才不會一直跳
function moveTo(bounds, { reveal = false, maxZoom = 14 } = {}) {
  clearTimeout(moveTimer);
  if (reveal) {
    // 飛得遠時 canvas 只畫得到出發點附近，點和線等到了再淡入，不要最後才突然冒出來
    if (viewFor(bounds, maxZoom)?.far && !REDUCED_MOTION) hideScene();
    else showScene();
  }
  moveTimer = setTimeout(() => {
    const view = viewFor(bounds, maxZoom);
    if (!view || (view.settled && !flight)) {
      flight?.();
      if (sceneHidden) showScene();
      return;
    }
    endWheel();
    flight?.();
    halt();
    if (REDUCED_MOTION) { map.fitBounds(bounds, { ...view.options, animate: false }); return; }
    const duration = view.far ? 1 : 0.6;
    const land = () => { flight?.(); if (sceneHidden) showScene(); };
    // 分頁沒在繪製時動畫不會前進，地圖會停在半路；時間到還沒抵達就直接跳過去
    const guard = setTimeout(() => {
      flight?.();
      halt();
      map.fitBounds(bounds, { ...view.options, animate: false });
      if (sceneHidden) showScene();
    }, duration * 1000 + 500);
    flight = () => { clearTimeout(guard); map.off('moveend', land); flight = null; };
    map.once('moveend', land);
    map.flyToBounds(bounds, { ...view.options, duration });
  }, 250);
}

// ---------- 滾輪縮放 ----------
// Leaflet 內建的滾輪一次跳一整級；這裡每一幀往目標靠近，游標下的位置不動
const WHEEL_RATE = 0.006;     // 滾輪每像素縮放幾級
const PINCH_RATE = 0.022;     // 觸控板雙指開合的位移小，要放大一點
const WHEEL_EASE_MS = 70;
const wheel = { frame: 0, target: 0, last: 0, point: null, latlng: null };

function endWheel() {
  if (!wheel.frame) return;
  cancelAnimationFrame(wheel.frame);
  wheel.frame = 0;
  map._moveEnd(true);
}

function wheelFrame(now) {
  const dt = Math.min(50, now - wheel.last);
  wheel.last = now;
  const eased = wheel.target + (map.getZoom() - wheel.target) * Math.exp(-dt / WHEEL_EASE_MS);
  const done = Math.abs(wheel.target - eased) < 0.002;
  const zoom = done ? wheel.target : eased;
  const offset = wheel.point.subtract(map.getSize().divideBy(2));
  map._move(map.unproject(map.project(wheel.latlng, zoom).subtract(offset), zoom), zoom);
  // canvas 預設只縮放舊畫面，點和線會跟著變大變小；每幀重畫才維持原本粗細
  if (canvas._map) {      // 還沒畫過任何圖形時 canvas 不在地圖上
    canvas._reset();
    L.Util.cancelAnimFrame(canvas._redrawRequest);
    canvas._redraw();
  }
  if (done) { wheel.frame = 0; map._moveEnd(true); }
  else wheel.frame = requestAnimationFrame(wheelFrame);
}

if (!REDUCED_MOTION) {
  map.getContainer().addEventListener('wheel', (e) => {
    e.preventDefault();
    if (!wheel.frame) {
      stopMoving();
      if (sceneHidden) showScene();
      map._moveStart(true, false);
      wheel.target = map.getZoom();
      wheel.last = performance.now();
      wheel.point = null;
      wheel.frame = requestAnimationFrame(wheelFrame);
    }
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? map.getSize().y : 1;
    const step = Math.max(-1.2, Math.min(1.2, -e.deltaY * unit * (e.ctrlKey ? PINCH_RATE : WHEEL_RATE)));
    wheel.target = Math.max(map.getMinZoom(), Math.min(map.getMaxZoom(), wheel.target + step));
    // 游標沒動就沿用同一個經緯度：每次重算的捨入誤差放大後，畫面會慢慢偏掉
    const point = map.mouseEventToContainerPoint(e);
    if (!wheel.point || wheel.point.distanceTo(point) > 1) {
      wheel.point = point;
      wheel.latlng = map.containerPointToLatLng(point);
    }
  }, { passive: false });
  map.on('mousedown', endWheel);
}

// ---------- 引用 ----------
function setFilter(refs) {
  state.filter = refs;
  state.focus = refs.length === 1 ? refs[0] : null;
  state.panel = true;
  renderEvidence();
  applyFocus();
}

function renderEvidence() {
  const panel = $('#evidence');
  const numbers = state.active?.numbers;
  const all = Object.values(state.scene?.evidence ?? {})
    .map((e, i) => ({ ...e, no: numbers?.get(e.ref) ?? i + 1 }))
    .sort((a, b) => a.no - b.no);
  panel.hidden = !state.panel || !all.length;
  const list = $('#evidence-list');
  list.innerHTML = '';
  for (const e of state.filter ? all.filter((x) => state.filter.includes(x.ref)) : all) {
    const card = el('button', `card${state.focus === e.ref ? ' active' : ''}`);
    card.type = 'button';
    card.dataset.ref = e.ref;
    card.append(el('b', '', e.no), el('strong', '', e.title), el('p', '', e.text));
    card.addEventListener('click', () => focusRef(e.ref, true));
    list.append(card);
  }
}

function focusRef(ref, toggle = false) {
  state.focus = toggle && state.focus === ref ? null : ref;
  applyFocus();
  for (const card of document.querySelectorAll('.card')) card.classList.toggle('active', card.dataset.ref === state.focus);
  if (!state.focus) return;
  $(`.card[data-ref="${CSS.escape(state.focus)}"]`)?.scrollIntoView({ block: 'nearest', behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
  const ids = state.scene?.evidence?.[state.focus]?.place_ids ?? [];
  const points = state.scene.points.filter((p) => ids.includes(p.id));
  if (points.length) moveTo(L.latLngBounds(points.map((p) => [p.lat, p.lon])));
}

function lineLook({ line, style }) {
  const refs = state.focus ? [state.focus] : state.filter;
  if (!refs?.length) return { weight: style.weight, opacity: style.opacity };
  return line.refs.some((r) => refs.includes(r)) ? { weight: style.weight + 1, opacity: 1 } : { weight: style.weight, opacity: DIM / 2 };
}

function clearCites() {
  for (const cite of document.querySelectorAll('.cite.active')) cite.classList.remove('active');
}

function applyFocus() {
  clearCites();
  const refs = state.focus ? [state.focus] : state.filter;
  const ids = refs?.length ? new Set(refs.flatMap((r) => state.scene?.evidence?.[r]?.place_ids ?? [])) : null;
  const lit = (pt) => !ids || ids.has(pt.id);
  for (const { pt, layer } of drawn.dots) layer.setStyle({ opacity: lit(pt) ? 1 : DIM, fillOpacity: lit(pt) ? 1 : DIM });
  for (const { pt, layer } of drawn.pins) layer.setOpacity(lit(pt) ? 1 : DIM);
  for (const item of drawn.lines) item.path.setStyle(lineLook(item));
  const byLayer = new Map([...drawn.dots, ...drawn.pins].map((d) => [d.layer, d.pt]));
  for (const label of drawn.labels) {
    const pt = byLayer.get(label.layer);
    label.node.classList.toggle('dim', Boolean(ids) && (pt ? !lit(pt) : true));
  }
}

// ---------- 步驟 ----------
function select(step, { open = false } = {}) {
  state.active?.button?.classList.remove('active');
  state.active = step;
  step.button?.classList.add('active');
  if (open) state.panel = true;
  render(step.scene);
}

// 跟著最新進度時，沒東西可畫的步驟不清掉地圖
function follow(step, open = false) {
  if (state.follow && hasDrawing(step.scene)) select(step, { open });
}

function appendToThread(node) {
  const thread = $('#thread');
  const pinned = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 90;
  if (pending.parentNode === thread) thread.insertBefore(node, pending);
  else thread.append(node);
  if (pinned) thread.scrollTop = thread.scrollHeight;
}

function formatArgs(args) {
  return Object.entries(args ?? {}).map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join(', ');
}

function addCall(name, argsText, result, scene, isError) {
  const step = { scene: scene ?? emptyScene() };
  const box = el('div', `call${isError ? ' err' : ''}`);
  const head = el('button', 'call-head');
  head.type = 'button';
  head.append(el('b', '', name));
  if (argsText !== null) head.append(`(${argsText})`);
  head.addEventListener('click', () => { state.follow = false; select(step, { open: true }); });
  step.button = box;
  box.append(head);

  const text = result.trim();
  const full = isError ? `Error: ${text || '工具沒有回傳說明'}` : text;
  if (full) {
    const lines = full.split('\n');
    const short = lines.slice(0, PREVIEW_LINES).join('\n');
    const out = el('pre', 'call-out', short);
    box.append(out);
    appendToThread(box);
    const rest = lines.length - PREVIEW_LINES;
    if (rest > 0 || out.scrollWidth > out.clientWidth) {
      const label = rest > 0 ? `… +${rest} 行` : '… 展開';
      const more = el('button', 'call-more', label);
      more.type = 'button';
      more.addEventListener('click', () => {
        const open = box.classList.toggle('open');
        out.textContent = open ? full : short;
        more.textContent = open ? '收合' : label;
      });
      box.append(more);
    }
  } else {
    appendToThread(box);
  }
  follow(step);
}

// ---------- Markdown（先跳脫，只產生固定的幾種標籤） ----------
function inline(text) {
  return text
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
}

function tableHtml(rows) {
  const cells = (row) => row.trim().replace(/^\||\|$/g, '').split('|').map((c) => inline(c.trim()));
  const head = cells(rows[0]).map((c) => `<th>${c}</th>`).join('');
  const body = rows.slice(2).map((r) => `<tr>${cells(r).map((c) => `<td>${c}</td>`).join('')}</tr>`).join('');
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function markdown(source) {
  const lines = esc(source).split('\n');
  const isRow = (l) => /^\s*\|.*\|\s*$/.test(l ?? '');
  const isRule = (l) => /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(l ?? '');
  const isItem = (l) => /^\s*(?:[-*+]|\d+[.)])\s+/.test(l ?? '');
  const isBlock = (l) => /^#{1,6}\s/.test(l) || /^&gt;/.test(l) || isItem(l) || (isRow(l));
  let html = '', i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const heading = line.match(/^(#{1,6})\s+(.*)/);
    if (heading) {
      const level = Math.min(heading[1].length + 2, 6);
      html += `<h${level}>${inline(heading[2])}</h${level}>`; i++; continue;
    }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) { html += '<hr>'; i++; continue; }
    if (isRow(line) && isRule(lines[i + 1])) {
      const rows = [];
      while (isRow(lines[i])) rows.push(lines[i++]);
      html += tableHtml(rows); continue;
    }
    if (/^&gt;/.test(line)) {
      const quote = [];
      while (/^&gt;/.test(lines[i] ?? '')) quote.push(lines[i++].replace(/^&gt;\s?/, ''));
      html += `<blockquote>${inline(quote.join('<br>'))}</blockquote>`; continue;
    }
    if (isItem(line)) {
      const tag = /^\s*\d/.test(line) ? 'ol' : 'ul';
      let items = '';
      while (isItem(lines[i])) {
        const sub = /^\s{2,}/.test(lines[i]);
        items += `<li${sub ? ' class="sub"' : ''}>${inline(lines[i++].replace(/^\s*(?:[-*+]|\d+[.)])\s+/, ''))}</li>`;
      }
      html += `<${tag}>${items}</${tag}>`; continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && (!para.length || !isBlock(lines[i]))) para.push(lines[i++]);
    html += `<p>${inline(para.join('<br>'))}</p>`;
  }
  return html;
}

function withCitations(html, scene) {
  const numbers = new Map();
  const missing = new Set(scene?.missing ?? []);
  const chip = (_, type, id) => {
    const ref = `${type} ${id}`;
    if (!numbers.has(ref)) numbers.set(ref, numbers.size + 1);
    return `<button type="button" class="cite" data-ref="${ref}"${missing.has(ref) ? ' disabled' : ''}>${numbers.get(ref)}</button>`;
  };
  const out = html
    .replace(/\b(place_note|day_note|move|day)\s+(\d+-\d+)\b/g, chip)
    .replace(/\[((?:\s|、|,|，|;|；|<button [^>]*>\d+<\/button>)+)\]/g, '$1');
  return { html: out, numbers };
}

// ---------- 事件 ----------
const handlers = {
  split({ questions }) { state.multi = questions.length > 1; },
  itinerary({ summary, days, scene }) {
    if (!Array.isArray(days)) throw new StaleServer();
    if (hasDrawing(scene)) addCall('itinerary', null, [summary, ...days].join('\n'), scene, false);
  },
  question({ n, text }) {
    if (!state.multi) return;
    appendToThread(el('div', 'q-title', `${n}. ${text}`));
  },
  judgment({ text }) {
    const box = el('div', 'say md');
    box.innerHTML = markdown(text);
    appendToThread(box);
  },
  tool({ name, args, result, is_error: isError, scene }) {
    if (typeof result !== 'string') throw new StaleServer();
    addCall(name, formatArgs(args), result, scene, isError);
  },
  answer({ text, scene }) {
    const step = { scene: scene ?? emptyScene() };
    const body = el('article', 'answer md');
    const { html, numbers } = withCitations(markdown(text), step.scene);
    step.numbers = numbers;
    body.innerHTML = html;
    body.addEventListener('click', (e) => {
      const cite = e.target.closest('.cite');
      if (e.target.closest('.cite:disabled')) return;
      if (!cite && !getSelection().isCollapsed) return;   // 在選取文字
      if (state.active !== step) { state.follow = false; select(step, { open: true }); }
      else if (!state.panel) { state.panel = true; renderEvidence(); }
      if (!cite) return;
      if (state.filter) { state.filter = null; renderEvidence(); }
      focusRef(cite.dataset.ref);
      cite.classList.add('active');
    });
    appendToThread(body);
    state.follow = true;
    follow(step, true);
  },
  scene_error({ text }) { showError('這一步的地圖畫不出來', text); },
  error({ text }) { showError('回答時發生錯誤', text); },
};

function showError(explain, detail = '') {
  const box = el('div', 'error-msg');
  box.setAttribute('role', 'alert');
  box.append(el('b', '', 'Error'), el('span', '', explain));
  if (detail && detail !== explain) box.append(el('pre', '', detail));
  appendToThread(box);
}

// 一個事件畫壞了不該中斷整題；只有伺服器版本不對時才停
function dispatch(event) {
  try {
    handlers[event.type]?.(event);
  } catch (e) {
    if (e instanceof StaleServer) throw e;
    showError(`畫面處理 ${event.type} 時出錯`, e.message);
  }
}

function emptyScene() {
  return { points: [], areas: [], circles: [], lines: [], evidence: {}, missing: [] };
}

function syncComposer() {
  const input = $('#input'), send = $('#send');
  input.style.height = 'auto';
  input.style.height = `${input.scrollHeight + 2}px`;
  const running = Boolean(state.controller);
  send.textContent = running ? '■' : '↑';
  send.setAttribute('aria-label', running ? '停止' : '送出');
  send.classList.toggle('stop', running);
  send.disabled = !running && !input.value.trim();
  $('#clear').disabled = running;     // 回答中要先按停止
}

function clearAll() {
  state.follow = true;
  state.multi = false;
  state.panel = false;
  state.active = null;
  render(null);
  map.setView(HOME, HOME_ZOOM, { animate: false });
  $('#thread').replaceChildren();
  document.body.classList.add('is-empty');
  $('#input').focus();
}

async function ask(question) {
  document.body.classList.remove('is-empty');
  state.follow = true;
  state.multi = false;
  state.panel = false;
  state.active?.button?.classList.remove('active');
  state.active = null;
  render(null);
  $('#thread').append(el('div', 'user-msg', question), pending);
  $('#thread').scrollTop = $('#thread').scrollHeight;
  $('#input').value = '';
  const controller = new AbortController();
  state.controller = controller;
  syncComposer();
  try {
    const res = await fetch('/api/ask', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }), signal: controller.signal,
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${res.status}`);
    }
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      const parts = buffer.split('\n');
      buffer = parts.pop();
      for (const part of parts) {
        if (!part.trim()) continue;
        dispatch(JSON.parse(part));
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') appendToThread(el('div', 'stopped', '已停止'));
    else if (e instanceof StaleServer) showError('伺服器還在跑舊版程式，請重新啟動後再問一次');
    else if (e instanceof TypeError) showError('連不到伺服器，請確認它還在執行', e.message);
    else showError(e.message);
  } finally {
    controller.abort();     // 中途出錯時也要通知伺服器停掉，否則下一題會卡在「上一題還在跑」
    pending.remove();
    state.controller = null;
    syncComposer();
  }
}

// ---------- 介面 ----------
$('#composer').addEventListener('submit', (e) => {
  e.preventDefault();
  if (state.controller) { state.controller.abort(); return; }
  const question = $('#input').value.trim();
  if (question) ask(question);
});
$('#input').addEventListener('input', syncComposer);
$('#input').addEventListener('keydown', (e) => {
  // 選字中的 Enter 是確認選字，不是送出；回答中的 Enter 不算按停止
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    if (!state.controller) $('#composer').requestSubmit();
  }
});
for (const b of document.querySelectorAll('#examples button')) {
  b.addEventListener('click', () => { $('#input').value = b.textContent; syncComposer(); $('#input').focus(); });
}
$('#evidence-close').addEventListener('click', () => { state.panel = false; renderEvidence(); });
$('#clear').addEventListener('click', clearAll);
$('#input').focus();
