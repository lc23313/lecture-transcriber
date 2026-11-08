class PCM16 extends AudioWorkletProcessor{
  constructor(){super();this.phase=0;this.sum=0;this.count=0;this.samples=[];}
  process(inputs){const input=inputs[0]?.[0];if(!input)return true;
    for(const x of input){this.sum+=x;this.count++;this.phase+=16000;
      if(this.phase>=sampleRate){this.phase-=sampleRate;this.samples.push(Math.max(-32768,Math.min(32767,Math.round(this.sum/this.count*32767))));this.sum=0;this.count=0;}
      if(this.samples.length===1600){const bytes=new ArrayBuffer(3200);const view=new DataView(bytes);this.samples.forEach((v,i)=>view.setInt16(i*2,v,true));this.port.postMessage(bytes,[bytes]);this.samples=[];}
    }return true;
  }
}
registerProcessor('pcm16',PCM16);
