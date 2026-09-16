import init, {verify_artifact} from './pkg/abx_tlsnotary.js';
await init();
self.onmessage = ({data}) => {
  try { self.postMessage({ok:true,result:JSON.parse(verify_artifact(new Uint8Array(data.bytes),data.key))}); }
  catch(error) { self.postMessage({ok:false,error:String(error)}); }
};
self.postMessage({ready:true});
