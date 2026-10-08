
const ICONS={
plus:'<path d="M12 5v14M5 12h14"/>',
layers:'<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
code:'<path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-14-2 18"/>',
globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18Z"/>',
compass:'<circle cx="12" cy="12" r="9"/><path d="m16 8-2.5 5.5L8 16l2.5-5.5L16 8Z"/>',
settings:'<path d="m9 3-.7 2.1-2.2 1.3-2.1-.4-2 3.4 1.5 1.7v2.6L2 15.4l2 3.4 2.1-.4 2.2 1.3L9 22h4l.7-2.3 2.2-1.3 2.1.4 2-3.4-1.5-1.7v-2.6L20 9.4 18 6l-2.1.4-2.2-1.3L13 3Z"/><circle cx="11" cy="12.5" r="3"/>',
shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
play:'<path d="m7 4 14 8-14 8Z"/>',
pause:'<path d="M8 5v14M16 5v14" stroke-width="3"/>',
document:'<path d="M14 3H5v18h14V8l-5-5Z"/><path d="M14 3v6h5M8 13h8M8 17h5"/>',
route:'<circle cx="5" cy="5" r="2"/><circle cx="19" cy="19" r="2"/><path d="M7 5h9a4 4 0 0 1 0 8H8a3 3 0 0 0 0 6h9"/>',
activity:'<path d="M3 12h4l3-7 4 14 3-7h4"/>',
check:'<path d="m5 12 4 4L19 6"/>',
arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
arrowUp:'<path d="M12 20V4m-6 6 6-6 6 6"/>',
warning:'<path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5m0 3v.1"/>',
info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.1"/>',
message:'<path d="M21 4H3v13h5v4l5-4h8V4Z"/><path d="M7 8h10M7 12h7"/>',
link:'<path d="m10 13 4-4m-6 7-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 1 1-1a4 4 0 1 1 6 6l-4 4a4 4 0 0 1-6 0" transform="translate(1 0)"/>',
search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
panel:'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M10 3v18m4-13h3m-3 4h3m-3 4h3"/>',
spark:'<path d="m12 3 2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2L12 3Z"/>',
close:'<path d="m6 6 12 12M6 18 18 6"/>',
chevron:'<path d="m9 5 7 7-7 7"/>',
exchange:'<path d="M3 7h17m-4-4 4 4-4 4M21 17H4m4-4-4 4 4 4"/>',
refresh:'<path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5"/>',
download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
menu:'<path d="M4 6h16M4 12h16M4 18h16"/>'
};

const emptySession=()=>({id:'',title:'安全研究',objective:'提供网站或代码，从一个值得研究的问题开始。',mode:'',status:'empty',stage:0,next:'',stack:'',budget:{},usage:{},findings:[],events:[],assets:[],evidence:[]});
document.addEventListener('alpine:init',()=>Alpine.data('workbench',()=>reportWorkspace(comparisonWorkspace(sourceWorkspace({
 projects:[],session:emptySession(),selectedId:'',view:'research',selectedFindingId:null,selectedEvidenceId:null,evidenceTab:'overview',evidenceOpen:false,menuOpen:false,
 findingFilter:'全部',assetSearch:'',hint:'',toast:'',toastTimer:null,formError:'',connectionError:'',loading:true,busy:false,pollTimer:null,selectionEpoch:0,
 runtime:{available:null,message:'正在检查执行器'},draft:{},scopeEpoch:0,scopeDraft:{},budgetDraft:{},artifactRaw:false,identityState:{items:[],import_directory:'',formats:[]},identityDraft:{label:'',source_file:'',id:null},identityError:'',identityLoading:false,
 async init(){window.addEventListener('hashchange',()=>this.loadRoute());await this.refreshList();await this.loadRoute();await this.refresh();this.loading=false;this.pollTimer=setInterval(()=>{if(!document.hidden&&!this.busy)this.refresh();},3000);},
 destroy(){clearInterval(this.pollTimer);clearTimeout(this.toastTimer);},
 async api(path,options={}){const response=await fetch('/api/research'+path,{...options,headers:{'Content-Type':'application/json',...options.headers}});if(!response.ok){let data;try{data=await response.json();}catch{};const detail=data?.detail;throw new Error(typeof detail==='string'?detail:detail?JSON.stringify(detail):'服务请求失败（'+response.status+'）');}return response.status===204?null:response.json();},
 get current(){return this.session},
 get selectedFinding(){const f=this.current.findings.find(f=>f.id===this.selectedFindingId)||null;return this.selectedEvidenceId&&!f?.evidence.some(e=>e.id===this.selectedEvidenceId)?null:f},
 get selectedEvidence(){return this.current.evidence.find(e=>e.id===this.selectedEvidenceId)||null},
 get reportView(){return this.reportSnapshot?.projection||{title:'研究报告',objective:'',status:'',findings:[],evidence:[],scope:{},limits:{},next:''}},
 get reportLink(){return this.reportSnapshot?location.origin+'/research#/'+encodeURIComponent(this.reportSnapshot.session_id)+'?report='+encodeURIComponent(this.reportSnapshot.id):''},
 get codeArtifact(){const e=this.selectedEvidence;return !!e&&['code','source','file'].includes(e.kind)},
 get artifactLines(){const e=this.selectedEvidence;if(!e||!e.content)return [];const start=e.metadata?.line_start,content=String(e.content),lines=content.split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/);if(/(?:\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029])$/.test(content))lines.pop();return lines.map((text,index)=>({key:index,n:Number.isInteger(start)&&start>0?start+index:null,text}))},
 get artifactHttp(){const e=this.selectedEvidence;if(!e||!['http','request','response'].includes(e.kind))return null;const m=e.metadata||{};return m.request!==undefined||m.response!==undefined?{request:m.request,response:m.response,method:m.method,url:m.url,status:m.status_code}:null},
 evidenceLabel(e){return ({code:'源码记录',source:'源码记录',file:'文件记录',http:'HTTP 记录',request:'请求记录',response:'响应记录',tool:'工具输出'})[e?.kind]||'研究记录'},
 get identityCount(){return this.identityState.items.filter(i=>i.status!=='revoked').length},
 get identitiesEditable(){return !['running','pause_requested'].includes(this.current.status)},
 identityKind(kind){return ({account:'账号密码',basic:'HTTP Basic',headers:'请求头',cookies:'Cookie'})[kind]||kind},
 identityStatus(identity){return ({active:'已导入',stale:'环境已变更',revoked:'已撤销'})[identity.status]||identity.status},
 async fetchIdentities(id=this.selectedId,epoch=this.selectionEpoch){if(!id)return;try{const data=await this.api('/sessions/'+encodeURIComponent(id)+'/identities');if(epoch!==this.selectionEpoch||id!==this.selectedId)return;this.identityState=data;this.identityError='';}catch(e){if(epoch===this.selectionEpoch)this.identityError=e.message}},
 async openIdentities(){if(!this.current.id)return;this.identityError='';this.identityDraft={label:'',source_file:'',id:null};this.identityLoading=true;document.getElementById('identity-dialog').showModal();await this.fetchIdentities();this.identityLoading=false},
 replaceIdentity(identity){this.identityDraft={id:identity.id,label:identity.label,source_file:''};this.identityError='';this.$nextTick(()=>document.querySelector('[aria-label="身份材料文件名"]').focus())},
 async saveIdentity(){if(this.busy||!this.current.url||!this.identitiesEditable)return;this.busy=true;this.identityError='';const id=this.selectedId,epoch=this.selectionEpoch,d={...this.identityDraft};try{const path='/sessions/'+encodeURIComponent(id)+'/identities';await this.api(d.id?path+'/'+encodeURIComponent(d.id)+'/replace':path,{method:'POST',body:JSON.stringify(d.id?{source_file:d.source_file.trim()}:{label:d.label.trim(),source_file:d.source_file.trim()})});if(epoch!==this.selectionEpoch)return;this.identityDraft={label:'',source_file:'',id:null};await this.fetchIdentities(id,epoch);await this.fetchSession(id,epoch);this.notify(d.id?'身份材料已更换，原有研究记录保留':'测试身份已导入，尚未验证登录状态')}catch(e){if(epoch===this.selectionEpoch)this.identityError=e.message}finally{this.busy=false}},
 async revokeIdentity(identity){if(this.busy||!this.identitiesEditable)return;this.busy=true;this.identityError='';const id=this.selectedId,epoch=this.selectionEpoch;try{await this.api('/sessions/'+encodeURIComponent(id)+'/identities/'+encodeURIComponent(identity.id),{method:'DELETE'});await this.fetchIdentities(id,epoch);await this.fetchSession(id,epoch);if(this.identityDraft.id===identity.id)this.identityDraft={label:'',source_file:'',id:null};this.notify('身份材料已撤销，历史记录保留')}catch(e){if(epoch===this.selectionEpoch)this.identityError=e.message}finally{this.busy=false}},
 get filteredFindings(){return this.current.findings.filter(f=>this.findingFilter==='全部'||this.verdictLabel(f.state)===this.findingFilter)},
 get filteredAssets(){const q=this.assetSearch.trim().toLowerCase();return this.current.assets.filter(a=>(a.name+' '+a.detail).toLowerCase().includes(q))},
 get statusTitle(){if(this.current.status==='queued'&&this.runtime.available===false)return '项目已保存，执行器尚未就绪';if(this.current.status==='queued'&&this.runtime.available===null)return '项目已保存，执行状态待确认';return ({empty:'从一个问题开始',queued:'研究已排队',running:'Agent 正在研究',pause_requested:'正在停止当前步骤',paused:'研究已暂停',waiting_input:'需要补充材料或调整边界',completed:'本轮研究已收束',failed:'研究执行受阻'})[this.current.status]||'状态未知'},
 get statusDescription(){if(this.current.latest_error)return this.current.latest_error;if(this.current.status==='queued'&&this.runtime.available===null)return this.runtime.message||'暂时无法确认执行器状态，项目与授权已保存。';if(this.current.status==='queued'&&this.runtime.available===false)return this.runtime.message||'执行器尚未就绪，项目与授权已保存。';return ({empty:'代码语言与框架由 Agent 识别。',queued:'等待执行器领取，尚未开始目标测试。',running:'进展与证据来自实际执行记录。',pause_requested:'停止确认后显示为已暂停，现有证据会保留。',paused:'继续使用原授权与剩余预算。',waiting_input:'查看研究记录，补齐条件后可以继续。',completed:'报告保留当前结论及未覆盖的方向。',failed:'已有记录保留，可查看错误后重试。'})[this.current.status]||''},
 get canResume(){return ['paused','waiting_input','completed','failed'].includes(this.current.status)},
 get budgetLabel(){const b=this.current.budget,u=this.current.usage;return Math.floor((u.elapsed_seconds||0)/60)+' / '+(b.minutes||0)+' 分钟 · '+(u.requests||0)+' / '+(b.requests||0)+' 请求 · $'+Number(u.cost_usd||0).toFixed(3)+' / $'+(b.max_cost_usd||0)},
 icon(name){return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(ICONS[name]||ICONS.info)+'</svg>'},
 modeLabel(mode){return ({combined:'黑白盒联动',code:'代码审计',web:'黑盒研究'})[mode]||'研究空间'},
 verdictLabel(state){return ({confirmed:'已证实',pending:'待验证',rejected:'已排除'})[state]||'暂无法判断'},
 statusLabel(status){return ({running:'研究中',queued:'已排队',paused:'已暂停',pause_requested:'正在暂停',completed:'已结束',waiting_input:'等待补充',failed:'执行受阻',empty:'尚无项目'})[status]||status},
 notify(message){this.toast=message;clearTimeout(this.toastTimer);this.toastTimer=setTimeout(()=>this.toast='',4200)},
 pretty(value){return typeof value==='string'?value:JSON.stringify(value,null,2)},
 normalize(data){const p={...emptySession(),...data};p.stack=Array.isArray(p.stack)?p.stack.join(' · '):p.stack;p.evidence=p.evidence||[];p.findings=(p.findings||[]).map(f=>({...f,state:f.status,summary:f.description,basis:(f.evidence_ids||[]).length+' 份关联证据',gap:f.limitations||'未提供额外限制说明。',severity:'',evidence:(f.evidence_ids||[]).map(id=>p.evidence.find(e=>e.id===id)).filter(Boolean).map(e=>({...e,label:e.kind,text:e.metadata?.path||e.metadata?.url||e.time,tab:'raw'}))}));p.events=(p.events||[]).map(e=>({...e,finding:e.finding_id,trace:e.detail?this.pretty(e.detail):'',time:e.time?new Date(e.time).toLocaleString('zh-CN',{hour12:false}):''}));p.assets=(p.assets||[]).map(a=>({...a,name:a.value,detail:a.source||'',type:a.kind==='repo'?'code':a.kind,label:a.kind==='repo'?'源码':a.kind==='url'?'网站':'材料'}));return p},
 async refreshList(){try{const data=await this.api('/sessions');this.projects=data.items;this.connectionError='';}catch(e){this.connectionError=e.message;}},
 async loadRoute(){try{const [rawId,query='']=location.hash.replace(/^#\/?/,'').split('?');const id=decodeURIComponent(rawId);if(id){await this.selectProject(id,false);const reportId=new URLSearchParams(query).get('report');if(reportId&&this.current.id===id)await this.openReport(reportId);}else if(this.projects.length){await this.selectProject(this.projects[0].id)}}catch(e){this.connectionError='项目链接无效：'+e.message}},
 async refresh(){await this.refreshList();try{this.runtime=await this.api('/runtime')}catch(e){this.runtime={available:null,message:'暂时无法确认执行器状态：'+e.message}};if(this.selectedId)await this.fetchSession(this.selectedId,this.selectionEpoch);},
 async fetchSession(id,epoch){try{const data=await this.api('/sessions/'+encodeURIComponent(id));if(epoch!==this.selectionEpoch||id!==this.selectedId)return;this.session=this.normalize(data);if(!this.session.findings.some(f=>f.id===this.selectedFindingId))this.selectedFindingId=this.session.findings[0]?.id||null;this.connectionError='';}catch(e){if(epoch===this.selectionEpoch)this.connectionError=e.message;}},
 async selectProject(id,updateHash=true){this.closeDialog('scope-dialog');this.resetComparison();this.resetSources();this.resetReports();document.getElementById('report-dialog')?.close();this.selectionEpoch++;this.selectedId=id;document.getElementById('identity-dialog')?.close();this.identityState={items:[],import_directory:'',formats:[]};this.identityDraft={label:'',source_file:'',id:null};this.identityError='';this.session=emptySession();this.view='research';this.evidenceOpen=false;this.menuOpen=false;this.selectedFindingId=null;this.selectedEvidenceId=null;this.findingFilter='全部';this.assetSearch='';this.hint='';if(updateHash)history.replaceState(null,'','#/'+encodeURIComponent(id));await this.fetchSession(id,this.selectionEpoch);await this.fetchIdentities(id,this.selectionEpoch);await this.fetchSources(id,this.selectionEpoch);},
 selectFinding(id){if(!this.current.findings.some(f=>f.id===id))return;this.selectedFindingId=id;this.selectedEvidenceId=null;this.evidenceTab='overview';this.evidenceOpen=window.matchMedia('(max-width:1020px)').matches;},
 openEvidence(id){if(!this.current.evidence.some(e=>e.id===id))return;this.artifactRaw=false;this.selectedEvidenceId=id;this.evidenceTab='raw';this.evidenceOpen=window.matchMedia('(max-width:1020px)').matches;},
 openAsset(){this.openScope()},
 openCreate(){this.formError='';this.draft={title:'',objective:'',url:'',repo:'',authorized:false,minutes:45,requests:300,max_cost_usd:2,max_steps:20};this.menuOpen=false;document.getElementById('create-dialog').showModal()},
 openScope(){if(!this.current.id||this.current.id!==this.selectedId)return;this.scopeEpoch++;this.formError='';this.scopeDraft={url:this.current.url||'',repo:this.current.repo||'',authorized:false};this.budgetDraft={...this.current.budget,authorized:false};document.getElementById('scope-dialog').showModal()},

 closeDialog(id){if(id==='scope-dialog'){this.scopeEpoch++;this.scopeDraft={};this.budgetDraft={};this.formError='';}if(id==='identity-dialog'){this.identityDraft={label:'',source_file:'',id:null};this.identityError='';}if(id==='report-dialog'){this.resetReports();if(this.selectedId)history.replaceState(null,'','#/'+encodeURIComponent(this.selectedId));}document.getElementById(id)?.close()},
 closeLayers(){this.evidenceOpen=false;this.menuOpen=false;document.querySelectorAll('dialog[open]').forEach(d=>this.closeDialog(d.id))},
 async createProject(){if(this.busy)return;this.busy=true;this.formError='';try{const d=this.draft;const result=await this.api('/sessions',{method:'POST',body:JSON.stringify({title:d.title.trim(),objective:d.objective.trim(),url:d.url.trim()||null,repo:d.repo.trim()||null,authorization_confirmed:d.authorized,budget:{minutes:d.minutes,requests:d.requests,max_cost_usd:d.max_cost_usd,max_steps:d.max_steps}})});await this.refreshList();await this.selectProject(result.id);this.closeDialog('create-dialog');this.notify('项目与授权已保存');if(result.repo)await this.captureSource();}catch(e){this.formError=e.message}finally{this.busy=false}},
 async action(name,payload){if(this.busy||!this.current.id)return false;this.busy=true;const id=this.selectedId,epoch=this.selectionEpoch;try{await this.api('/sessions/'+encodeURIComponent(id)+'/'+name,{method:'POST',body:JSON.stringify(payload||{})});await this.fetchSession(id,epoch);await this.refreshList();return id===this.selectedId&&epoch===this.selectionEpoch}catch(e){if(id===this.selectedId&&epoch===this.selectionEpoch)this.notify(e.message);return false}finally{this.busy=false}},
 async togglePause(){await this.action(this.canResume?'resume':'pause')},
 async sendHint(){const draft=this.hint,content=draft.trim(),id=this.selectedId,epoch=this.selectionEpoch;if(!content)return;if(await this.action('hints',{content})&&id===this.selectedId&&epoch===this.selectionEpoch){if(this.hint===draft)this.hint='';this.notify('研究方向已保存，执行器将在下一步读取')}},
 scopeContextMatches(id,epoch,scopeEpoch){return id===this.selectedId&&epoch===this.selectionEpoch&&scopeEpoch===this.scopeEpoch&&document.getElementById('scope-dialog')?.open},
 scopeDraftSnapshot(){return JSON.stringify([this.scopeDraft,this.budgetDraft])},
 async saveMaterials(){
  if(this.busy||!this.current.id||this.current.id!==this.selectedId||!document.getElementById('scope-dialog')?.open)return;
  this.busy=true;this.formError='';
  const id=this.selectedId,epoch=this.selectionEpoch,scopeEpoch=this.scopeEpoch,drafts=this.scopeDraftSnapshot();
  try{
   await this.api('/sessions/'+encodeURIComponent(id)+'/materials',{method:'PATCH',body:JSON.stringify({url:this.scopeDraft.url.trim()||null,repo:this.scopeDraft.repo.trim()||null,authorization_confirmed:this.scopeDraft.authorized})});
   await this.fetchSession(id,epoch);await this.refreshList();await this.fetchIdentities(id,epoch);await this.fetchSources(id,epoch);
   if(this.scopeContextMatches(id,epoch,scopeEpoch)){
    if(this.scopeDraftSnapshot()===drafts)this.closeDialog('scope-dialog');
    this.notify('材料与授权版本已更新');
   }
  }catch(e){if(this.scopeContextMatches(id,epoch,scopeEpoch))this.formError=e.message}finally{this.busy=false}
 },
 async saveBudget(){
  if(this.busy||!this.current.id||this.current.id!==this.selectedId||!document.getElementById('scope-dialog')?.open)return;
  this.busy=true;this.formError='';
  const id=this.selectedId,epoch=this.selectionEpoch,scopeEpoch=this.scopeEpoch,drafts=this.scopeDraftSnapshot();
  try{
   const {authorized,...budget}=this.budgetDraft;
   await this.api('/sessions/'+encodeURIComponent(id)+'/budget',{method:'PATCH',body:JSON.stringify({budget,authorization_confirmed:authorized})});
   await this.fetchSession(id,epoch);await this.refreshList();
   if(this.scopeContextMatches(id,epoch,scopeEpoch)){
    if(this.scopeDraftSnapshot()===drafts)this.closeDialog('scope-dialog');
    this.notify('预算已更新，已用额度保留');
   }
  }catch(e){if(this.scopeContextMatches(id,epoch,scopeEpoch))this.formError=e.message}finally{this.busy=false}
 },
 async finishResearch(){if(await this.action('complete'))this.notify(this.reportSnapshot?'本轮已结束；当前快照保持原样，可生成最新快照':'本轮研究已收束')},
 async requestRetest(){if(!this.selectedFinding)return;this.hint='针对发现 '+this.selectedFinding.id+'（'+this.selectedFinding.title+'）进行复测；保留原证据，比较当前版本与行为，并记录变化。';this.evidenceOpen=false;this.notify('复测方向已填入，发送后在当前预算内执行')},

})))));
