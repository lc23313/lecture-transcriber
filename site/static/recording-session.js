/* A transport belongs to one recording. Late events cannot affect another recording. */
export class RecordingSession {
  constructor(meta){this.meta=meta;this.ended=false;this.socket=null;this.retry=null;this.closeTimer=null;this.frames=[];this.maxFrames=100;this.lines=new Map();this.queue=Promise.resolve();this.pending=0;this.failed=false;this.unsaved=[];this.accumulated=0;this.ready=false;this.sentStart=false;}
  buffer(data,start){this.frames.push({data,start});if(this.frames.length>this.maxFrames){this.frames.shift();this.meta.realtimeGap=true;}}
  drain(){const connection=this.socket;if(!this.ready||connection?.readyState!==1)return;
    while(this.frames.length&&connection.bufferedAmount<64000){const frame=this.frames.shift();if(!this.sentStart){connection.send(JSON.stringify({type:'audio-start',offset:Math.max(0,Math.round(frame.start))}));this.sentStart=true;}connection.send(frame.data);}}
  finish(){this.ended=true;clearTimeout(this.retry);this.ready=false;const connection=this.socket;
    if(connection?.readyState===1){connection.send(JSON.stringify({type:'finish'}));this.closeTimer=setTimeout(()=>connection.close(),20000);}else connection?.close();}
}
