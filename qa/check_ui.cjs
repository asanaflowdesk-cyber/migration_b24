const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");

const assets = "processes/flowdesk_chatbot/internal_admin_assets/";
const html = fs.readFileSync(assets + "index.html", "utf8");
const script = fs.readFileSync(assets + "app.js", "utf8");
const graphScript = fs.readFileSync(assets + "graph.js", "utf8");
const {connect,layout} = require("../" + assets + "graph.js");
const location = new URL("http://127.0.0.1:" + process.env.FIXTURE_PORT + "/local/tools/deskflow-admin/index.php");

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.className = "";
    this.style = {};
    this.dataset = {};
    this.textContent = "";
    this.value = "";
    this.hidden = false;
    this.classList = {
      contains: value => this.className.split(/\s+/).includes(value),
      add: value => this.classList.toggle(value, true),
      remove: value => this.classList.toggle(value, false),
      toggle: (value, force) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        if (force ?? !names.has(value)) names.add(value);
        else names.delete(value);
        this.className = [...names].join(" ");
      }
    };
  }
  append(...nodes) {
    for (const node of nodes) {
      if (node instanceof Element) node.parentElement = this;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  get childElementCount() { return this.children.length; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, fn) { this["on" + key] = fn; }
  querySelector(selector) { return descendants(this).slice(1).find(node => matches(node,selector)) || null; }
  getBoundingClientRect() { return {left:0,top:0,right:20,height:20}; }
  getContext() { return Object.fromEntries(["scale","beginPath","moveTo","bezierCurveTo","stroke","lineTo","fill"].map(name=>[name,()=>{}])); }
  scrollTo(coords) { this.scrollLeft=coords.left;this.scrollTop=coords.top; }
  showModal() { this.open=true; }
  close() { this.open=false; }
  remove() { this.parentElement.children=this.parentElement.children.filter(node=>node!==this); }
  focus() {}
  setSelectionRange() {}
  dispatchEvent(event) { this["on"+event.type]?.(event); }
}

function matches(node,selector) {
  if(selector.startsWith("."))return node.classList.contains(selector.slice(1));
  if(selector.startsWith("[data-node="))return node.dataset.node===selector.split('"')[1];
  return node.tagName===selector;
}

function start(uid) {
  const ids = {};
  const tabs = [];
  const sections = [];
  for (const match of html.matchAll(/<(\w+)([^<>]*)>/g)) {
    const [, tag, attrs] = match;
    const id = attrs.match(/\bid="([^"]+)"/)?.[1];
    const tab = attrs.match(/data-tab="([^"]+)"/)?.[1];
    if (!id && !tab) continue;
    const node = new Element(tag);
    node.hidden = /\shidden(?:\s|$)/.test(attrs);
    node.className = attrs.match(/class="([^"]+)"/)?.[1] || "";
    if (id) { ids[id] = node; node.id = id; }
    if (tab) { node.dataset.tab = tab; tabs.push(node); }
    if (node.classList.contains("tab")) sections.push(node);
  }
  Object.assign(ids["graph-viewport"],{clientWidth:700,clientHeight:500,scrollLeft:0,scrollTop:0});
  const events={};
  const document = {
    getElementById: id => ids[id] || null,
    querySelector: () => ({content: "csrf-" + uid}),
    querySelectorAll: selector => selector === "[data-tab]" ? tabs : selector === ".tab" ? sections : descendants(ids["graph-scene"]).filter(node=>matches(node,selector)),
    createElement: tag => new Element(tag),
    createTextNode: text => text
  };
  const fetchForUser = (url, options) => fetch(url, {
    ...options, headers: {...options.headers, Cookie: "test_uid=" + uid}
  });
  const context=vm.createContext({
    document, window:{addEventListener:(event,handler)=>events[event]=handler,confirm:()=>true},
    fetch:fetchForUser,location,URL,structuredClone,crypto:webcrypto,Event,devicePixelRatio:1,
    Option:function(label,value){const node=new Element("option");node.textContent=label;node.value=value;return node;},
    setTimeout:()=>0,clearTimeout:()=>{},requestAnimationFrame:callback=>setImmediate(callback)
  });
  vm.runInContext(graphScript,context);
  vm.runInContext(script,context);
  return {ids,tabs,events};
}

async function waitFor(predicate, message) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 15));
  }
  throw new Error(message);
}
function descendants(element) {
  if (!(element instanceof Element)) return [];
  return [element, ...element.children.flatMap(descendants)];
}
function text(element) {
  if (typeof element === "string") return element;
  return element.textContent + element.children.map(text).join("");
}

const findInput=(area,label)=>descendants(area).find(item=>item.attributes["aria-label"]===label);
const button=(area,label)=>descendants(area).find(item=>item.tagName==="button"&&item.textContent===label);

(async () => {
  const model={start:"a",nodes:[{id:"a",next:"b"},{id:"b",next:"c"},{id:"c"}]};
  assert.throws(()=>connect(model,{source:"c",kind:"next"},"a"),/замкнёт/);
  assert.equal(connect(model,{source:"a",kind:"next"},"c").nodes[0].next,"c");
  assert.ok(layout(model).get("c").x>layout(model).get("b").x);

  const owner=start(101), ui=owner.ids;
  const tab=name=>owner.tabs.find(item=>item.dataset.tab===name).onclick();
  await waitFor(()=>ui.preview.childElementCount>0,"preview did not load");
  assert.equal(ui["access-tab"].hidden,false);
  const card=ui["graph-scene"].querySelector('[data-node="start"]');
  assert.ok(card);
  const initial=card.style.left;
  card.querySelector("button").onpointerdown({button:0,clientX:0,clientY:0,preventDefault(){}});
  owner.events.pointermove({clientX:60,clientY:20});
  owner.events.pointerup({clientX:60,clientY:20});
  assert.notEqual(ui["graph-scene"].querySelector('[data-node="start"]').style.left,initial);

  const title=findInput(ui.editor,"Текст вопроса / сообщения");
  title.value="Новое приветствие";title.oninput();
  await ui.save.onclick();
  assert.match(ui["version-status"].textContent,/Редакция 2/);
  await tab("mapping");
  ui["add-mapping"].onclick();
  const template=findInput(ui["mapping-content"],"Шаблон значения");
  template.value="Тип {{request_type}}\nОтвет {{description}}";template.oninput();
  await ui.publish.onclick();
  assert.match(ui["version-status"].textContent,/В боте версия 1/);
  await tab("testing");
  assert.match(text(ui.preview),/Новое приветствие/);
  await button(ui.preview,"Другое").onclick();
  ui["preview-text"].value="Проверка задачи";await ui["preview-send"].onclick();
  await button(ui.preview,"Не применимо").onclick();
  await button(ui.preview,"Пропустить").onclick();
  await button(ui.preview,"Создать обращение").onclick();
  assert.match(text(ui["preview-task"]),/Тип Другое\nОтвет Проверка задачи/);

  await tab("journal");
  assert.match(text(ui["journal-content"]),/303/);
  const item=descendants(ui["journal-content"]).find(node=>node.tagName==="button");
  await item.onclick();
  assert.equal(ui["request-dialog"].open,true);
  assert.match(text(ui["request-detail"]),/Тестовый экран сотрудника/);
  ui["request-close"].onclick();
  await tab("versions");await ui.publish.onclick();
  await button(ui["versions-content"],"Восстановить и применить").onclick();
  assert.match(ui["version-status"].textContent,/В боте версия 3/);

  const ordinary=start(303).ids;
  await waitFor(()=>!ordinary.blocked.hidden,"ordinary employee could read the editor");
  assert.ok(ordinary.workspace.hidden);
  await ui["access-tab"].onclick();
  ui["grant-user"].value="202";
  await ui["grant-access"].onsubmit({preventDefault(){}});
  assert.match(text(ui["access-list"]),/Редактор · ID 202/);
  const editor=start(202).ids;
  await waitFor(()=>editor.preview.childElementCount>0,"granted editor could not open");
  assert.equal(editor["access-tab"].hidden,true);
  await button(ui["access-list"],"Отозвать").onclick();
  await editor.publish.onclick();
  assert.ok(editor.workspace.hidden);
  assert.equal(editor.blocked.hidden,false);
  console.log("Editor fixture passed: graph, save, publish, task preview, journal, rollback, grant and revoke");
})().catch(error=>{console.error(error);process.exitCode=1;});
