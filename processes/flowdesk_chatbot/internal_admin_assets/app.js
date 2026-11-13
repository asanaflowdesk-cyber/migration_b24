"use strict";
(() => {
 const $ = id => document.getElementById(id);
 const csrf = document.querySelector('meta[name="deskflow-csrf"]').content;
 let doc, revision, selected, dirty = false, preview, map;
 let taskFields = {}, activeVersion = null, previewStale = true, journalOffset = 0;
 const kinds={menu:"Меню",single:"Один вариант",multi:"Несколько вариантов",text:"Текст",number:"Число",date:"Дата",files:"Вложения и ссылки",info:"Инструкция",review:"Проверка и отправка",end:"Успешное завершение"};
 const operators={eq:"Равно",ne:"Не равно",contains:"Содержит",not_contains:"Не содержит",in:"Один из",exists:"Заполнено",not_exists:"Не заполнено",gt:"Больше",gte:"Не меньше",lt:"Меньше",lte:"Не больше"};
 const styles={secondary:"Обычная",primary:"Основная",alert:"Акцент / предупреждение",base:"Нейтральная"};
 const uid=prefix=>prefix+"_"+crypto.randomUUID().replaceAll("-","").slice(0,16);
 const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
 const notify=(s)=>{$("notice").textContent=s;$("notice").classList.add("show");clearTimeout(notify.timer);notify.timer=setTimeout(()=>$("notice").classList.remove("show"),6000);};
 function changed() {
   dirty = true;
   previewStale = true;
   $("dirty").textContent = "Есть несохранённые изменения";
   if (doc && selected) showConnections();
   map?.render();
 }
 async function api(path, data) {
   const freezing = data !== undefined && ["draft", "graph", "publish", "rollback", "preview"].includes(path);
   if (freezing) $("workspace").inert = true;
   try {
     const url = new URL(location.pathname, location.origin);
     url.searchParams.set("r", "/api/" + path);
     const response = await fetch(url, {
       method: data === undefined ? "GET" : "POST",
       credentials: "same-origin",
       cache: "no-store",
       headers: {"X-DeskFlow-CSRF": csrf, "Content-Type": "application/json"},
       body: data === undefined ? undefined : JSON.stringify(data)
     });
     if (response.status === 404) {
       $("blocked").hidden = false;
       $("workspace").hidden = true;
       $("navigation").hidden = true;
       throw new Error("Страница недоступна");
     }
     const result = await response.json();
     if (!response.ok) {
       if (response.status === 400) {
         $("errors").hidden=false; $("errors").textContent=result.error || "Проверьте настройки";
       }
       throw new Error(result.error || "Ошибка запроса");
     }
     return result;
   } finally {
     if (freezing) $("workspace").inert = false;
   }
 }
 const safe=fn=>(...args)=>Promise.resolve().then(()=>fn(...args)).catch(e=>notify(e.message));
 function apply(result) {
   doc = result.document;
   revision = result.revision;
   activeVersion = result.active_version;
   if (result.task_fields) taskFields = result.task_fields;
   previewStale = true;
   $("errors").hidden = true;
   dirty = false;
   $("dirty").textContent = "";
   $("version-status").textContent = "Редакция " + revision + (activeVersion ? " · В боте версия " + activeVersion : " · Сценарий ещё не применён");
   if (selected && doc.nodes.some(n => n.id === selected)) {
     tree();
     editor();
   }
   if ($("settings").classList.contains("active")) showSettings();
   if ($("mapping").classList.contains("active")) showMapping();
   map?.render();
 }
 const node=()=>doc.nodes.find(n=>n.id===selected);
 function field(parent,label,value,set,type="text",mapping=false){
   const l=el("label",label),input=el(type==="textarea"?"textarea":"input");
   input.setAttribute("aria-label",label);
   if(type!=="textarea")input.type=type;
   input.value=value??"";input.addEventListener("input",()=>{set(input.type==="number"?(input.value===""?undefined:Number(input.value)):input.value);changed();});
   l.append(input);parent.append(l);
   if (mapping) l.classList.add("task-template");
   if (type === "textarea") addVariables(l, input);
   return l;
 }
 function select(parent,label,value,choices,set){
   const l=el("label",label),input=el("select");
   input.setAttribute("aria-label",label);
   for(const [v,t] of Object.entries(choices)){const o=el("option",t);o.value=v;input.append(o);}
   input.value=value??"";input.addEventListener("change",()=>{set(input.value);changed();});
   l.append(input);parent.append(l);return l;
 }
 function check(parent,label,value,set){
   const l=el("label"),input=el("input");input.type="checkbox";input.checked=!!value;input.addEventListener("change",()=>{set(input.checked);changed();});l.append(input,document.createTextNode(label));parent.append(l);
 }
 const destinations=(blank=true)=>Object.fromEntries([...(blank?[["","По умолчанию"]]:[]),...doc.nodes.map(n=>[n.id,(n.section||"Без раздела")+" · "+(n.label||n.title).slice(0,90)])]);
 function tree(){
   const term=$("node-search").value.toLowerCase(),area=$("tree");area.replaceChildren();
   const groups=new Map();
   for(const n of doc.nodes){if(term&&!((n.label||"")+" "+n.title+" "+n.section).toLowerCase().includes(term))continue;const g=n.section||"Без раздела";if(!groups.has(g))groups.set(g,[]);groups.get(g).push(n);}
   for(const [group,nodes] of groups){area.append(el("div",group,"tree-group"));for(const n of nodes){
     const b=el("button",undefined,"tree-node"+(n.id===selected?" active":"")+(n.enabled===false?" off":""));b.append(el("span","","dot"),el("span",(n.label||n.title).slice(0,100)));b.draggable=true;b.dataset.nodeId=n.id;
     b.onclick=()=>selectNode(n.id);
     b.ondragstart=e=>e.dataTransfer.setData("text/plain",n.id);
     b.ondragover=e=>e.preventDefault();
     b.ondrop=safe(async e=>{e.preventDefault();const key=e.dataTransfer.getData("text/plain");if(key===n.id)return;apply(await api("graph",{revision,document:doc,action:"move",node_id:key,target:n.id}));tree();editor();notify("Порядок вопросов изменён");});
     area.append(b);
   }}
 }
 function conditions(parent,obj,key="show_if"){
   const box=el("details"),summary=el("summary","Условия показа");box.append(summary);
   const c=obj[key] || {};
   box.open = Object.values(c).some(rules => rules.length);
   const bindings = Object.fromEntries(bindingChoices());
   for(const group of ["all","any"]){
     const block=el("div",undefined,"conditions");block.append(el("div",group==="all"?"Все условия одновременно":"Хотя бы одно условие","muted"));
     const rules=c[group] || [];
     for(const [index,rule] of rules.entries()){
       const row=el("div",undefined,"condition-row");
       select(row,"Ответ",rule.field,bindings,v=>rule.field=v);
       select(row,"Условие",rule.op,operators,v=>{rule.op=v;rule.value=v==="in"?String(rule.value??"").split(";").map(x=>x.trim()):String(rule.value??"");editor();});
       field(row,"Значение",Array.isArray(rule.value)?rule.value.join("; "):rule.value,v=>{rule.value=rule.op==="in"?v.split(";").map(s=>s.trim()):v;});
       const del=el("button","×");del.onclick=()=>{rules.splice(index,1);changed();editor();};row.append(del);block.append(row);
     }
     const add=el("button","＋ Условие");add.onclick=()=>{obj[key]=c;c[group]=rules;rules.push({field:Object.keys(bindings)[0],op:"eq",value:""});changed();editor();};block.append(add);box.append(block);
   }
   parent.append(box);
 }
 function editor(){
   showConnections();
   const n=node(),area=$("editor");if(!n)return;area.replaceChildren();$("editor-heading").textContent=n.label||"Настройки шага";
   const grid=el("div",undefined,"field-grid");
   field(grid,"Название шага в конструкторе",n.label,v=>{n.label=v;tree();});
   field(grid,"Раздел",n.section,v=>{n.section=v;tree();});
   select(grid,"Тип ответа",n.kind,kinds,v=>{n.kind=v;if(["info","review","end"].includes(v))n.input_mode="buttons";editor();});
   select(grid,"Ввод сотрудника",n.input_mode,{buttons:"Только кнопки",text:"Только текст",both:"Кнопки и текст"},v=>n.input_mode=v);
   field(grid,"Текст вопроса / сообщения",n.title,v=>n.title=v,"textarea").classList.add("full");
   field(grid,"Описание, подсказка, пример",n.hint,v=>n.hint=v,"textarea").classList.add("full");
   select(grid,"Следующий шаг / иначе",n.next,destinations(),v=>n.next=v||null).classList.add("full");
   select(grid,"Куда идти, если окно скрыто",n.skip_next,destinations(),v=>n.skip_next=v||null).classList.add("full");area.append(grid);
   const checks=el("div",undefined,"checks");
   for(const [key,label] of [["enabled","Шаг включён"],["required","Ответ обязателен"],["allow_back","Назад"],["allow_save","Черновик"],["allow_cancel","Отмена"]])check(checks,label,n[key]!==false,v=>n[key]=v);area.append(checks);
   check(checks,"Развилка без показа окна",n.hidden_router,v=>n.hidden_router=v);
   conditions(area,n);
   const advanced=el("details");advanced.append(el("summary","Хранение ответа и поле результата"));
   field(advanced,"Имя ответа для зависимостей",n.bind,v=>n.bind=v);
   const registry = Object.fromEntries(Object.entries(taskFields).filter(([key])=>key.startsWith("UF_")));
   if(n.registry_field && !registry[n.registry_field]) registry[n.registry_field]=n.registry_field;
   select(advanced,"Поле задачи для ответа",n.registry_field,{"":"Без отдельного поля",...registry},v=>n.registry_field=v);
   area.append(advanced);
   area.append(el("h3","Проверка ответа","section-title"));const limits=n.limits||(n.limits={}),lg=el("div",undefined,"field-grid");
   const map={text:[["min_length","Минимум символов"],["max_length","Максимум символов"]],number:[["min","Минимальное число"],["max","Максимальное число"]],multi:[["min_choices","Минимум вариантов"],["max_choices","Максимум вариантов"]],files:[["max_files","Максимум файлов"],["max_file_mb","Размер одного файла, МБ"]]};
   for(const [key,label] of map[n.kind]||[]){field(lg,label,limits[key],v=>{if(v===undefined)delete limits[key];else limits[key]=v;},"number");}
   if(n.kind==="date")for(const [key,label] of [["min_date","Не раньше"],["max_date","Не позже"]])field(lg,label,limits[key],v=>limits[key]=v,"date");
   if(n.kind==="text")select(lg,"Готовая проверка",limits.validator||"",{"":"Обычный текст",bin_iin:"БИН / ИИН: 12 цифр"},v=>limits.validator=v);
   if(n.kind==="files")field(lg,"Форматы через запятую (пусто — любые)",(limits.extensions||[]).join(", "),v=>limits.extensions=v.split(",").map(x=>x.trim()).filter(Boolean)).classList.add("full");
   area.append(lg,el("h3","Варианты ответа и кнопки","section-title"));
   (n.options||[]).forEach((o,index)=>{
     const box=el("div",undefined,"option"),og=el("div",undefined,"field-grid");
     field(og,"Текст кнопки",o.text,v=>o.text=v);
     select(og,"Действие",o.action||"choice",{choice:"Ответ и переход",link:"Открыть ссылку",download:"Скачать файл Б24",back:"Вернуться назад",create_task:"Создать задачу",save_draft:"Сохранить черновик",finish:"Завершить без задачи",cancel:"Отменить обращение"},v=>{o.action=v;editor();});
     if (["link","download"].includes(o.action)) {
       field(og,o.action === "download" ? "Путь скачивания с Диска Б24" : "Ссылка HTTPS или путь Б24",o.link,v=>o.link=v).classList.add("full");
     } else if (o.action === "create_task") {
       const endings=Object.fromEntries(doc.nodes.filter(item=>item.kind==="end").map(item=>[item.id,item.label || item.title]));
       select(og,"Окно после создания задачи",o.next,{"":"Выберите окно",...endings},v=>o.next=v||null);
     } else if ((o.action || "choice") === "choice") {
       field(og,"Сохраняемый ответ",o.value || o.text,v=>o.value=v);
       if(n.kind!=="multi")select(og,"Переход",o.next,destinations(),v=>o.next=v||null);
     }
     select(og,"Стиль",o.style||"",{"":"Общий стиль",...styles},v=>o.style=v);
     select(og,"Расположение",o.display||"LINE",{LINE:"В строке",BLOCK:"На всю строку"},v=>o.display=v);
     field(og,"Ширина, пиксели (необязательно)",o.width,v=>{if(v===undefined)delete o.width;else o.width=v;},"number");
     const colors=el("details");colors.append(el("summary","Свои цвета"));
     for(const [key,label] of [["bg_color","Фон"],["text_color","Текст"],["off_bg_color","Фон после нажатия"],["off_text_color","Текст после нажатия"]]) {
       const row=field(colors,label,o[key],v=>o[key]=v);
       const picker=el("input");picker.type="color";picker.value=o[key] || "#ffffff";
       picker.setAttribute("aria-label",label+" — палитра");
       picker.oninput=()=>{row.querySelector("input").value=picker.value;o[key]=picker.value;changed();};row.append(picker);
     }
     og.append(colors);box.append(og);
     const cs=el("div",undefined,"checks");check(cs,"Включена",o.enabled!==false,v=>o.enabled=v);check(cs,"Свой вариант",o.other,v=>o.other=v);if(n.kind==="multi")check(cs,"Исключает другие ответы",o.exclusive,v=>o.exclusive=v);box.append(cs);conditions(box,o);
     const acts=el("div",undefined,"option-actions");
     const up=el("button","↑"),down=el("button","↓"),del=el("button","Удалить","danger");
     up.disabled=index===0;down.disabled=index===n.options.length-1;
     up.onclick=()=>{[n.options[index-1],n.options[index]]=[n.options[index],n.options[index-1]];changed();editor();};
     down.onclick=()=>{[n.options[index+1],n.options[index]]=[n.options[index],n.options[index+1]];changed();editor();};
     del.onclick=()=>{n.options.splice(index,1);changed();editor();};acts.append(up,down,del);box.append(acts);area.append(box);
   });
   const add=el("button","＋ Добавить кнопку");add.onclick=()=>{(n.options||(n.options=[])).push({id:uid("o"),text:"Новый вариант",value:"Новый вариант",style:"secondary",display:"LINE",enabled:true,action:"choice"});changed();editor();};area.append(add);
   {
     area.append(el("h3","Условные переходы","section-title"));
     for(const route of n.routes||[]){const r=el("div",undefined,"option");select(r,"Куда перейти",route.next,destinations(),v=>route.next=v);conditions(r,route,"when");const remove=el("button","Удалить переход");remove.onclick=()=>{n.routes.splice(n.routes.indexOf(route),1);changed();editor();};r.append(remove);area.append(r);}
     const plus=el("button","＋ Условный переход");plus.onclick=()=>{(n.routes||(n.routes=[])).push({next:n.next,when:{}});changed();editor();};area.append(plus);
   }
 }
 async function save(publish=false){
   apply(await api(publish ? "publish" : "draft",{document:doc,revision}));tree();
   notify(publish ? "Версия " + activeVersion + " применяется к новым обращениям" : "Черновик сохранён");
   if ($("versions").classList.contains("active")) await showVersions();
 }
 async function validate(){const result=await api("validate",{document:doc});$("errors").hidden=!result.errors.length;$("errors").textContent=result.errors.join("\n");if(!result.errors.length)notify("Переходы и настройки проверены");return result.errors;}
 async function insert(copy=false){
   const n=node(),id=uid("q"),newNode=copy?structuredClone(n):{label:"Новый вопрос",title:"Введите вопрос",hint:"",kind:"text",input_mode:"both",required:true,enabled:true,options:[],limits:{max_length:10000},allow_back:true,allow_save:true,allow_cancel:true,section:n.section};
   newNode.id=id;newNode.bind="answers."+id;delete newNode.registry_field;delete newNode.routes;delete newNode.hidden_router;
   if(copy)for(const o of newNode.options){o.id=uid("o");o.next=null;}
   apply(await api("graph",{revision,document:doc,action:"insert",target:n.id,node:newNode}));selectNode(id);notify("Вопрос добавлен в черновик");
 }
 function bb(text){
   const root=el("div",undefined,"preview-bubble");const stack=[root],tags={b:"strong",i:"em",u:"u",s:"s"};
   for(const part of String(text).split(/(\[\/?[bius]\])/i)){const tag=part.match(/^\[(\/?)([bius])\]$/i);if(!tag){stack.at(-1).append(document.createTextNode(part));continue;}if(tag[1]){if(stack.length>1)stack.pop();}else{const item=el(tags[tag[2].toLowerCase()]);stack.at(-1).append(item);stack.push(item);}}
   return root;
 }
 function showPreview(result){
   if(result.errors?.length){$("errors").hidden=false;$("errors").textContent=result.errors.join("\n");return;}
   preview=result.session;const screen=result.screen,area=$("preview");area.replaceChildren(bb(screen.text));
   if(result.result?.status==="validation_error")area.append(el("p",result.result.message,"danger"));
   const buttons=el("div",undefined,"preview-buttons");
   for(const b of screen.buttons){const button=el("button",b.label,b.display==="BLOCK"?"block":"");if(b.bg_color)button.style.backgroundColor=b.bg_color;else if(b.style==="primary"){button.style.backgroundColor="#2467df";button.style.color="white";}else if(b.style==="alert"){button.style.backgroundColor="#ffe1e6";button.style.color="#b5374e";}if(b.text_color)button.style.color=b.text_color;if(b.width)button.style.width=b.width+"px";button.onclick=safe(async()=>{if(b.link){notify("Кнопка откроет ссылку: "+b.link);return;}showPreview(await api("preview",{session:preview,action:b.action}));});buttons.append(button);}area.append(buttons);
   $("preview-input").hidden=!screen.accepts_text;$("preview-file-label").hidden=!screen.accepts_files;
   if(result.task_preview) showPairs($("preview-task"),Object.entries(result.task_preview).map(([key,value])=>[taskFields[key] || key,value]));
 }
 async function startPreview(){
   $("preview-task").replaceChildren();
   const result=await api("preview",{document:doc});
   showPreview(result);
   previewStale=!!result.errors?.length;
 }
 function showSettings(){
   const area=$("settings-content");area.replaceChildren();const settings=doc.settings;
   const common=el("div",undefined,"card");common.append(el("h2","Общие кнопки"));
   for(const [key,value] of Object.entries(settings.labels))field(common,{back:"Назад",save:"Черновик",cancel:"Отмена",skip:"Пропуск",continue:"Продолжение",submit:"Отправка",edit:"Исправление",restart:"Новое обращение",resume:"Продолжить черновик",review:"К проверке",files_replace:"Замена вложений",files_clear:"Удаление вложений",retry:"Повторная передача",check_task:"Проверка наличия задачи",history_more:"История обращений",launcher_open:"Открытие служебного чата",launcher_ready:"Переход в служебный чат"}[key]||key,value,v=>settings.labels[key]=v);area.append(common);
   const other=el("div",undefined,"card");other.append(el("h2","Поведение и результат"));
   field(other,"Прерванное заполнение: часов бездействия",settings.idle_hours,v=>settings.idle_hours=v,"number");
   field(other,"Максимум черновиков сотрудника",settings.max_drafts,v=>settings.max_drafts=v,"number");
   check(other,"Показывать историю обращений сотрудника",settings.show_history!==false,v=>settings.show_history=v);
   for(const [key,value] of Object.entries(settings.messages))field(other,{saved:"После сохранения черновика",cancelled:"После отмены",error:"При ошибке передачи",other_prompt:"Уточнение своего варианта",edit_prompt:"Выбор ответа для исправления",confirmed:"После подтверждения отправки",launcher:"Активация бота",launcher_ready:"Чат готов к работе"}[key]||key,value,v=>settings.messages[key]=v,"textarea");
   for(const [key,value] of Object.entries(settings.styles))select(other,{choice:"Стиль вариантов",back:"Стиль «Назад»",submit:"Стиль отправки",cancel:"Стиль отмены"}[key]||key,value,styles,v=>settings.styles[key]=v);
   const output=settings.output||(settings.output={});field(other,"Проект Б24: ID (пусто — текущая настройка)",output.project_id,v=>{if(v===undefined)delete output.project_id;else output.project_id=v;},"number");field(other,"Ответственный: ID (пусто — текущая настройка)",output.responsible_id,v=>{if(v===undefined)delete output.responsible_id;else output.responsible_id=v;},"number");
   area.append(other);
 }
 $("node-search").oninput=tree;$("save").onclick=safe(()=>save());$("publish").onclick=safe(()=>save(true));$("validate").onclick=safe(validate);
 $("add-node").onclick=safe(()=>insert());$("copy-node").onclick=safe(()=>insert(true));
 $("new-node").onclick=()=>{
   const id=uid("q");
   doc.nodes.push({id,label:"Новое окно",title:"Введите сообщение",hint:"",kind:"info",input_mode:"buttons",enabled:true,required:false,section:node().section,options:[],bind:"answers."+id});
   selectNode(id);changed();notify("Соедините новое окно с нужной карточкой на схеме");
 };
 $("delete-node").onclick=()=>{$("delete-replacement").replaceChildren();for(const [value,text] of Object.entries(destinations(false))){if(value===selected)continue;const o=el("option",text);o.value=value;$("delete-replacement").append(o);}$("delete-replacement").value=node().next||doc.start;$("delete-dialog").showModal();};
 $("delete-abort").onclick=()=>$("delete-dialog").close();$("delete-confirm").onclick=safe(async()=>{const replacement=$("delete-replacement").value;apply(await api("graph",{revision,document:doc,action:"delete",node_id:selected,replacement}));$("delete-dialog").close();selectNode(replacement);});
 $("start-preview").onclick=safe(startPreview);$("preview-send").onclick=safe(async()=>{const text=$("preview-text").value;$("preview-text").value="";showPreview(await api("preview",{session:preview,text}));});
 $("preview-file").onchange=safe(async()=>{const f=$("preview-file").files[0];if(!f)return;showPreview(await api("preview",{session:preview,files:[{id:Math.floor(Math.random()*1e8)+1,name:f.name,size:f.size}]}));$("preview-file").value="";});
 window.addEventListener("beforeunload",event=>{if(dirty){event.preventDefault();event.returnValue="";}});
 function showConnections() {
   const area = $("connections");
   const current = node();
   area.replaceChildren();
   if (!current) return;

   const from = el("span", current.label || current.title, "connection-origin");
   const list = el("div", undefined, "connection-targets");
   const edges = [
     ...(current.next ? [{next: current.next, label: "Далее"}] : []),
     ...(current.options || []).filter(option => option.enabled !== false && option.next)
       .map(option => ({next: option.next, label: option.text})),
     ...(current.routes || []).filter(route => route.next)
       .map(route => ({next: route.next, label: "По условию"}))
   ];
   for (const edge of edges) {
     const target = doc.nodes.find(candidate => candidate.id === edge.next);
     if (!target) continue;
     const button = el("button", edge.label + " → " + (target.label || target.title));
     button.onclick = () => {
       selectNode(target.id);
     };
     list.append(button);
   }
   if (!list.childElementCount) return;
   area.append(from, el("span", "→", "connection-arrow"), list);
 }

 function selectNode(id, focus=true) {
   selected=id;
   tree();editor();
   document.querySelectorAll(".map-card").forEach(card=>card.classList.toggle("selected",card.dataset.node===id));
   if(focus && map){map.render();map.focus();}
 }

 function bindingChoices() {
   const choices=new Map();
   for(const item of doc.nodes) {
     if(!item.bind) continue;
     choices.set(item.bind,(item.label || item.bind)+" · "+item.bind);
     if(item.options?.some(o=>o.other)) choices.set(item.bind+".other",(item.label || item.bind)+" · уточнение");
   }
   for(const [key,label] of Object.entries({"system.user_id":"ID сотрудника","system.request_id":"ID обращения","system.task_id":"Номер задачи"})) choices.set(key,label);
   return choices;
 }

 function addVariables(parent,input) {
   const row=el("div",undefined,"variable-picker"), choices=el("select"), add=el("button","Вставить ответ");
   choices.setAttribute("aria-label","Какой ответ вставить");
   for(const [value,label] of bindingChoices()){
     if(parent.classList.contains("task-template") && value==="system.task_id") continue;
     const option=el("option",label);option.value=value;choices.append(option);
   }
   add.type="button";
   add.onclick=()=>{
     const token="{{"+choices.value+"}}", first=input.selectionStart ?? input.value.length, last=input.selectionEnd ?? first;
     input.value=input.value.slice(0,first)+token+input.value.slice(last);
     input.dispatchEvent(new Event("input"));input.focus();input.setSelectionRange(first+token.length,first+token.length);
   };
   row.append(choices,add);parent.append(row);
 }

 function showMapping() {
   const area=$("mapping-content");area.replaceChildren();
   const rows=doc.settings.mapping ||= {};
   for(const [name,spec] of Object.entries(rows)){
     const card=el("div",undefined,"card mapping-row");
     const available=Object.fromEntries(Object.entries(taskFields).filter(([key])=>!(key in rows)||key===name));
     if(!available[name])available[name]=name;
     select(card,"Поле задачи",name,available,value=>{delete rows[name];rows[value]=spec;showMapping();});
     field(card,"Шаблон значения",spec.template,value=>spec.template=value,"textarea",true);
     check(card,"Все подставляемые ответы обязательны",spec.required,value=>spec.required=value);
     const remove=el("button","Удалить настройку","danger");
     remove.onclick=()=>{delete rows[name];changed();showMapping();};card.append(remove);area.append(card);
   }
   if(!area.childElementCount)area.append(el("p","Используются готовые поля DeskFlow. Добавьте поле, чтобы задать своё значение."));
 }
 $("add-mapping").onclick=()=>{
   const rows=doc.settings.mapping ||= {};
   const name=["DESCRIPTION",...Object.keys(taskFields)].find(key=>!(key in rows));
   if(!name){notify("Все доступные поля уже добавлены");return;}
   rows[name]={template:"",required:false};changed();showMapping();
 };

 const statuses={in_progress:"Заполняется",draft:"Черновик",interrupted:"Прервано",cancelled:"Отменено",information:"Завершено без задачи",pending:"Передаётся",needs_check:"Требует проверки",failed:"Ошибка передачи",created:"Задача создана"};
 const events={screen:"Показано окно",text:"Введён текст",files:"Прикреплены файлы",button:"Нажата кнопка",resume:"Возобновлено",confirmed:"Подтверждено",task_send:"Передача в Б24",task_created:"Задача создана",task_unknown:"Требует проверки",task_rejected:"Ошибка передачи",link_requested:"Открыта ссылка",download_requested:"Запрошено скачивание",interrupted:"Прервано"};
 const stamp=value=>value ? new Date(value*1000).toLocaleString("ru-RU") : "";
 function table(parent,headings){
   const wrap=el("div",undefined,"table-wrap"),grid=el("table"),head=el("thead"),row=el("tr"),body=el("tbody");
   for(const title of headings)row.append(el("th",title));
   head.append(row);grid.append(head,body);wrap.append(grid);parent.append(wrap);return body;
 }
 function showPairs(parent,pairs){
   parent.replaceChildren();
   for(const [name,value] of pairs){
     const item=el("div",undefined,"answer-pair");
     item.append(el("strong",name),el("pre",typeof value==="object"?JSON.stringify(value,null,2):String(value)));parent.append(item);
   }
 }
 async function showJournal(){
   const result=await api("journal",{query:$("journal-query").value,status:$("journal-status").value,offset:journalOffset});
   const area=$("journal-content");area.replaceChildren();
   const body=table(area,["Обращение","Сотрудник","Сценарий и этап","Статус","Изменено","Задача"]);
   for(const item of result.items){
     const row=el("tr"),id=el("td"),button=el("button",item.request_id.slice(0,12));
     button.onclick=safe(()=>showRequest(item.request_id));id.append(button);
     row.append(id,el("td",item.user_id),el("td",[item.scenario,item.step].filter(Boolean).join(" · ")),el("td",statuses[item.status]||item.status),el("td",stamp(item.updated_at)),el("td",item.task_id || ""));body.append(row);
   }
   if(!result.items.length)area.append(el("p","Обращений по этому фильтру нет."));
   $("journal-back").disabled=journalOffset===0;
   $("journal-next").disabled=journalOffset+result.items.length>=result.count;
 }
 async function showRequest(id,after=0){
   const result=await api("journal/request",{request_id:id,after});
   const area=$("request-detail");
   if(!after){
     area.replaceChildren(el("p",result.request_id+" · ID "+result.user_id+" · "+(statuses[result.status]||result.status)));
     if(result.error)area.append(el("p",result.error,"danger"));
     const answers=el("div");showPairs(answers,result.answers);area.append(answers,el("h3","События"));
   }
   area.querySelector(".load-more")?.remove();
   for(const event of result.events){
     const block=el("div",undefined,"event-row");
     block.append(el("strong",[stamp(event.at),events[event.kind]||event.kind,event.step].filter(Boolean).join(" · ")));
     const details=event.detail;
     const lines=[details.text,details.button,details.message,details.url,details.task_id ? "Задача № "+details.task_id : "",...(details.files||[]).map(file=>file.name||"Файл № "+file.id)].filter(Boolean);
     if(lines.length)block.append(el("pre",lines.join("\n")));area.append(block);
   }
   if(result.next){const more=el("button","Ещё события","load-more");more.onclick=safe(()=>showRequest(id,result.next));area.append(more);}
   if(!$("request-dialog").open)$("request-dialog").showModal();
 }
 $("request-close").onclick=()=>$("request-dialog").close();
 $("journal-filter").onsubmit=safe(async event=>{event.preventDefault();journalOffset=0;await showJournal();});
 $("journal-back").onclick=safe(async()=>{journalOffset=Math.max(0,journalOffset-50);await showJournal();});
 $("journal-next").onclick=safe(async()=>{journalOffset+=50;await showJournal();});
 async function showVersions(){
   const result=await api("versions");
   const area=$("versions-content");area.replaceChildren();
   const body=table(area,["Версия","Применена","Автор","Восстановлена из","Действие"]);
   for(const item of result.versions){
     const row=el("tr"),cell=el("td"),button=el("button",item.id===activeVersion?"В боте":"Восстановить и применить");
     button.disabled=item.id===activeVersion;
     button.onclick=safe(async()=>{
       if(!window.confirm("Применить версию "+item.id+"? Текущий черновик будет заменён."))return;
       apply(await api("rollback",{version:item.id,revision}));selectNode(doc.start);await showVersions();notify("Восстановленная версия применена");
     });
     cell.append(button);row.append(el("td",item.id),el("td",stamp(item.applied_at)),el("td",item.author),el("td",item.restored_from||""),cell);body.append(row);
   }
   if(!result.versions.length)area.append(el("p","После первого применения здесь появится версия сценария."));
 }

 async function showAccess(result) {
   const model = result || await api("access");
   const area = $("access-list");
   area.replaceChildren(el("p", "Владелец · ID " + model.owner_id));
   for (const editor of model.editors) {
     const row = el("div", undefined, "access-row");
     const remove = el("button", "Отозвать");
     remove.onclick = safe(async () => {
       await showAccess(await api("access/revoke", {user_id: editor.user_id}));
       notify("Доступ отозван");
     });
     row.append(el("span", "Редактор · ID " + editor.user_id), remove);
     area.append(row);
   }
 }

 document.querySelectorAll("[data-tab]").forEach(button => {
   button.onclick = safe(async () => {
     document.querySelectorAll("[data-tab]").forEach(item => item.classList.toggle("selected", item === button));
     document.querySelectorAll(".tab").forEach(section => section.classList.toggle("active", section.id === button.dataset.tab));
     if (button.dataset.tab === "settings") showSettings();
     if (button.dataset.tab === "builder") {map.render();map.focus();}
     if (button.dataset.tab === "testing" && previewStale) await startPreview();
     if (button.dataset.tab === "mapping") showMapping();
     if (button.dataset.tab === "journal") await showJournal();
     if (button.dataset.tab === "versions") await showVersions();
     if (button.dataset.tab === "access") await showAccess();
   });
 });

 $("grant-access").onsubmit = safe(async event => {
   event.preventDefault();
   const input = $("grant-user");
   const result = await api("access/grant", {user_id: input.value.trim()});
   input.value = "";
   await showAccess(result);
   notify("Доступ выдан");
 });

 safe(async () => {
   const result = await api("draft");
   apply(result);
   $("identity").textContent = (result.identity.role === "owner" ? "Владелец" : "Редактор") + " · ID " + result.identity.id;
   $("access-tab").hidden = result.identity.role !== "owner";
   $("workspace").hidden = false;
   $("navigation").hidden = false;
   selected = doc.start;
   map=window.DeskFlowMap({getDocument:()=>doc,getSelected:()=>selected,select:selectNode,change:changed,notify,
     replace:value=>{doc=value;tree();editor();changed();}});
   selectNode(selected);
   await startPreview();
 })();
})();
