/* Saved report browsing is read-only. Creation is always an explicit action. */
function reportWorkspace(target) {
 return Object.defineProperties(target, Object.getOwnPropertyDescriptors({
  reportSnapshot:null,reportLoading:false,reportGenerating:false,reportError:'',reportEpoch:0,
  reportRequest:0,reportRetryId:null,reportHistory:[],reportHistoryCursor:null,reportHistoryMore:false,
  reportHistoryLoading:false,reportHistoryError:'',reportHistoryReady:false,reportHistoryRequest:0,
  reportHistoryOpen:false,
  resetReports(){
   this.reportEpoch++;this.reportRequest++;this.reportHistoryRequest++;
   this.reportSnapshot=null;this.reportLoading=false;this.reportGenerating=false;this.reportError='';
   this.reportRetryId=null;this.reportHistory=[];this.reportHistoryCursor=null;this.reportHistoryMore=false;
   this.reportHistoryLoading=false;this.reportHistoryError='';this.reportHistoryReady=false;this.reportHistoryOpen=false;
  },
  reportContextValid(id,epoch){return epoch===this.reportEpoch&&id===this.selectedId&&id===this.current.id},
  reportDate(value){return new Date(value).toLocaleString('zh-CN',{hour12:false})},
  acceptReport(snapshot){
   this.reportSnapshot=snapshot;
   history.replaceState(null,'','#/'+encodeURIComponent(snapshot.session_id)+'?report='+encodeURIComponent(snapshot.id));
  },
  async openReport(reportId=null){
   if(!this.current.id)return;
   this.resetReports();
   const id=this.current.id,epoch=this.reportEpoch,dialog=document.getElementById('report-dialog');
   if(!dialog.open)dialog.showModal();
   if(reportId){
    await this.loadReport(reportId);
    if(this.reportContextValid(id,epoch))await this.loadReportHistory(true);
   }else{
    const items=await this.loadReportHistory(true);
    if(this.reportContextValid(id,epoch)&&items?.length)await this.loadReport(items[0].id);
   }
  },
  async loadReportHistory(reset=false){
   if(this.reportHistoryLoading&&!reset)return null;
   if(!reset&&!this.reportHistoryMore)return null;
   const id=this.current.id,epoch=this.reportEpoch,request=++this.reportHistoryRequest;
   const cursor=reset?null:this.reportHistoryCursor;
   this.reportHistoryLoading=true;this.reportHistoryError='';
   try{
    const data=await this.api('/sessions/'+encodeURIComponent(id)+'/reports?limit=20'+(cursor?'&before='+encodeURIComponent(cursor):''));
    if(!this.reportContextValid(id,epoch)||request!==this.reportHistoryRequest)return null;
    const items=reset?[]:this.reportHistory,known=new Set(items.map(item=>item.id));
    this.reportHistory=[...items,...data.items.filter(item=>!known.has(item.id))];
    this.reportHistoryCursor=data.next_cursor;this.reportHistoryMore=data.has_more;this.reportHistoryReady=true;
    return data.items;
   }catch(e){
    if(this.reportContextValid(id,epoch)&&request===this.reportHistoryRequest)this.reportHistoryError=e.message;
    return null;
   }finally{
    if(this.reportContextValid(id,epoch)&&request===this.reportHistoryRequest)this.reportHistoryLoading=false;
   }
  },
  async loadReport(reportId){
   if(this.reportGenerating)return;
   const id=this.current.id,epoch=this.reportEpoch,request=++this.reportRequest;
   this.reportLoading=true;this.reportError='';this.reportRetryId=reportId;
   try{
    const snapshot=await this.api('/sessions/'+encodeURIComponent(id)+'/reports/'+encodeURIComponent(reportId));
    if(!this.reportContextValid(id,epoch)||request!==this.reportRequest)return;
    this.acceptReport(snapshot);this.reportRetryId=null;
   }catch(e){
    if(this.reportContextValid(id,epoch)&&request===this.reportRequest)this.reportError=e.message;
   }finally{
    if(this.reportContextValid(id,epoch)&&request===this.reportRequest)this.reportLoading=false;
   }
  },
  async generateReport(){
   if(this.reportLoading||this.reportGenerating||!this.current.id)return;
   const id=this.current.id,epoch=this.reportEpoch,request=++this.reportRequest;
   this.reportLoading=true;this.reportGenerating=true;this.reportError='';this.reportRetryId=null;
   try{
    const snapshot=await this.api('/sessions/'+encodeURIComponent(id)+'/reports',{method:'POST',body:'{}'});
    if(!this.reportContextValid(id,epoch)||request!==this.reportRequest)return;
    this.acceptReport(snapshot);
    await this.loadReportHistory(true);
   }catch(e){
    if(this.reportContextValid(id,epoch)&&request===this.reportRequest){
     this.reportError='未能确认生成结果。请先刷新历史报告，检查是否已保存。'+e.message;
     this.reportHistoryOpen=true;
    }
   }finally{
    if(this.reportContextValid(id,epoch)&&request===this.reportRequest){this.reportLoading=false;this.reportGenerating=false;}
   }
  },
  async exportReport(){
   if(!this.reportSnapshot){await this.openReport();return;}
   const snapshot=this.reportSnapshot;
   const blob=new Blob([snapshot.markdown],{type:'text/markdown;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');
   a.href=url;a.download='cairn-'+snapshot.session_id+'-'+snapshot.id+'.md';a.click();
   setTimeout(()=>URL.revokeObjectURL(url),1000);this.notify('已导出当前快照，内容与保存版本一致');
  }
 }));
}
