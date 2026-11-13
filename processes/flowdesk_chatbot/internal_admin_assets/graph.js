"use strict";
(() => {
  function edges(doc) {
    const result = [];
    for (const node of doc.nodes) {
      if (node.next) result.push({source: node.id, kind: "next", target: node.next});
      if (node.skip_next) result.push({source: node.id, kind: "skip_next", target: node.skip_next});
      for (const [index, route] of (node.routes || []).entries()) {
        if (route.next) result.push({source: node.id, kind: "routes", index, target: route.next});
      }
      for (const [index, option] of (node.options || []).entries()) {
        if (option.next && ["choice", "create_task"].includes(option.action || "choice")) {
          result.push({source: node.id, kind: "options", index, target: option.next});
        }
      }
    }
    return result;
  }

  function connect(doc, edge, target) {
    if (!doc.nodes.some(node => node.id === target)) throw new Error("Выберите существующее окно");
    const copy = structuredClone(doc);
    const from = copy.nodes.find(node => node.id === edge.source);
    if (edge.index === undefined) from[edge.kind] = target;
    else from[edge.kind][edge.index].next = target;
    const graph = edges(copy), checked = new Set(), pending = [target];
    while (pending.length) {
      const id = pending.pop();
      if (id === edge.source) throw new Error("Переход замкнёт сценарий. Для возврата есть действие «Назад».");
      if (checked.has(id)) continue;
      checked.add(id);
      pending.push(...graph.filter(item => item.source === id).map(item => item.target));
    }
    return copy;
  }

  function layout(doc) {
    const graph = edges(doc), depth = new Map([[doc.start, 0]]), queue = [doc.start];
    for (const id of queue) {
      for (const edge of graph) {
        if (edge.source !== id || depth.has(edge.target)) continue;
        depth.set(edge.target, depth.get(id) + 1);
        queue.push(edge.target);
      }
    }
    const rows = new Map();
    return new Map(doc.nodes.map(node => {
      const column = depth.get(node.id) || 0, row = rows.get(column) || 0;
      rows.set(column, row + 1);
      return [node.id, doc.layout?.[node.id] || {x: 26 + 285 * column, y: 26 + 380 * row}];
    }));
  }

  if (typeof module !== "undefined") module.exports = {edges, connect, layout};
  if (typeof window === "undefined") return;

  window.DeskFlowMap = function mount({getDocument, getSelected, select, change, replace, notify}) {
    const $ = id => document.getElementById(id);
    const view = $("graph-viewport"), scene = $("graph-scene"), canvas = $("graph-lines"), spacer = $("graph-spacer");
    let zoom = .8, drag, frame, points, cards, ports;
    const el = (tag, text, css) => {
      const item = document.createElement(tag);
      if (text) item.textContent = text;
      if (css) item.className = css;
      return item;
    };
    const portKey = edge => [edge.source, edge.kind, edge.index ?? ""].join(":");

    function redraw() {
      if (frame || !ports) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        const box = view.getBoundingClientRect(), scale = Math.min(devicePixelRatio || 1, 2);
        canvas.width = view.clientWidth * scale;
        canvas.height = view.clientHeight * scale;
        Object.assign(canvas.style, {width: view.clientWidth + "px", height: view.clientHeight + "px", left: view.scrollLeft + "px", top: view.scrollTop + "px"});
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        ctx.scale(scale, scale);
        function arrow(port, x, y, color) {
          const start = port.getBoundingClientRect(), sx = start.right - box.left, sy = start.top + start.height / 2 - box.top;
          const dx = Math.max(35, Math.abs(x - sx) / 2);
          ctx.beginPath(); ctx.moveTo(sx, sy); ctx.bezierCurveTo(sx + dx, sy, x - dx, y, x, y);
          ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke();
          ctx.fillStyle = color; ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - 7, y - 4); ctx.lineTo(x - 7, y + 4); ctx.fill();
        }
        for (const edge of edges(getDocument())) {
          const port = ports.get(portKey(edge)), card = cards.get(edge.target);
          if (!port || !card) continue;
          const target = card.getBoundingClientRect();
          arrow(port, target.left - box.left, target.top - box.top + 16, edge.kind === "skip_next" ? "#c78c50" : "#83a5c8");
        }
        if (drag?.edge) arrow(drag.port, drag.x - box.left, drag.y - box.top, "#2566cf");
      });
    }

    function port(node, kind, label, index) {
      const row = el("div", "", "map-row"), button = el("button", "→", "map-port");
      const edge = {source: node.id, kind, index};
      button.title = "Перетащите стрелку на окно назначения";
      button.setAttribute("aria-label", "Переход «" + label + "»");
      button.onpointerdown = event => {
        if (event.button !== 0) return;
        event.preventDefault();
        drag = {edge, port: button, x: event.clientX, y: event.clientY}; redraw();
      };
      row.append(el("span", label), button); ports.set(portKey(edge), button);
      return row;
    }

    function render() {
      const doc = getDocument();
      if (!doc) return;
      const section = $("map-section"), filter = section.value;
      const names = [...new Set(doc.nodes.map(n => n.section || "Без раздела"))];
      section.replaceChildren(new Option("Все разделы", ""), ...names.map(name => new Option(name, name)));
      section.value = names.includes(filter) ? filter : "";
      points = layout(doc); cards = new Map(); ports = new Map(); scene.replaceChildren();
      let width = 900, height = 620;
      for (const node of doc.nodes) {
        if (section.value && section.value !== (node.section || "Без раздела")) continue;
        const card = el("article", "", "map-card" + (node.id === getSelected() ? " selected" : "") + (node.enabled === false ? " off" : ""));
        card.dataset.node = node.id;
        const heading = el("button", node.label || node.id, "map-heading");
        heading.onclick = () => select(node.id, false);
        heading.onpointerdown = event => {
          if (event.button !== 0) return;
          event.preventDefault();
          select(node.id, false);
          drag = {node, card, point: {...points.get(node.id)}, x: event.clientX, y: event.clientY, moved: false};
        };
        const point = points.get(node.id);
        card.style.left = point.x + "px"; card.style.top = point.y + "px";
        card.append(heading, el("p", (node.title || "").replace(/\[[^\]]*\]/g, "").slice(0, 150), "map-text"));
        for (const [index, option] of (node.options || []).slice(0, 8).entries()) {
          const row = ["choice", "create_task"].includes(option.action || "choice") ? port(node, "options", option.text, index) : el("div", option.text, "map-row");
          if (option.bg_color) row.style.backgroundColor = option.bg_color;
          if (option.text_color) row.style.color = option.text_color;
          card.append(row);
        }
        if (node.options?.length > 8) card.append(el("p", "Ещё кнопок: " + (node.options.length - 8), "map-text"));
        for (const [index] of (node.routes || []).slice(0, 4).entries()) card.append(port(node, "routes", "Если · " + (index + 1), index));
        if (node.kind !== "end") card.append(port(node, "next", node.routes?.length ? "Иначе" : "Далее"));
        if (node.skip_next || node.enabled === false || Object.values(node.show_if || {}).some(rules => rules.length)) card.append(port(node, "skip_next", "При пропуске"));
        scene.append(card); cards.set(node.id, card);
        width = Math.max(width, point.x + 280); height = Math.max(height, point.y + 375);
      }
      scene.style.transform = `scale(${zoom})`;
      spacer.style.width = width * zoom + "px"; spacer.style.height = height * zoom + "px";
      $("map-zoom").textContent = Math.round(zoom * 100) + "%";
      redraw();
    }

    function focus() {
      if (!cards.has(getSelected())) { $("map-section").value = ""; render(); }
      const point = points.get(getSelected());
      if (point) view.scrollTo({left: Math.max(0, point.x * zoom - 20), top: Math.max(0, point.y * zoom - 20)});
    }
    window.addEventListener("pointermove", event => {
      if (!drag) return;
      if (drag.edge) { drag.x = event.clientX; drag.y = event.clientY; }
      else {
        const dx = (event.clientX - drag.x) / zoom, dy = (event.clientY - drag.y) / zoom;
        if (Math.abs(dx) + Math.abs(dy) < 4 && !drag.moved) return;
        drag.moved = true;
        const point = {x: Math.max(0, drag.point.x + dx), y: Math.max(0, drag.point.y + dy)};
        points.set(drag.node.id, point);
        drag.card.style.left = point.x + "px"; drag.card.style.top = point.y + "px";
      }
      redraw();
    });
    window.addEventListener("pointerup", event => {
      if (!drag) return;
      const done = drag; drag = null;
      try {
        if (done.edge) {
          const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-node]");
          if (target) replace(connect(getDocument(), done.edge, target.dataset.node));
        } else if (done.moved) {
          (getDocument().layout ||= {})[done.node.id] = points.get(done.node.id);
          change();
        }
      } catch (error) { notify(error.message); }
      render();
    });
    window.addEventListener("pointercancel", () => { drag = null; render(); });
    window.addEventListener("resize", redraw);
    view.addEventListener("scroll", redraw, {passive: true});
    $("map-section").onchange = render;
    $("map-focus").onclick = focus;
    $("map-plus").onclick = () => { zoom = Math.min(1.4, zoom + .1); render(); };
    $("map-minus").onclick = () => { zoom = Math.max(.25, zoom - .1); render(); };
    return {render, focus};
  };
})();
