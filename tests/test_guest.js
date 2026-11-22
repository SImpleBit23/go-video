'use strict';
const assert=require('assert'),cp=require('child_process'),path=require('path'),fs=require('fs'),os=require('os');
const {VKAccount,install}=require('../native/st-govideo-d/vk');
class D { onLine(){} }
install(D);
async function main(){
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'govideo-web-'));
 const events=[],d=new D();d.emit=x=>events.push(x);d.opts={stateDir:dir,fetcher:['python'],leaseFile:path.join(dir,'absent')};
 d.ytc=()=>dir;d.pyzArgs=()=>[];d.positions=()=>({});
 const runner=process.env.GO_VIDEO_HELPER_HOST;
 d.spawnFetchUid=(cmd,args)=>cp.spawn('python',[runner].concat(args),{stdio:['pipe','pipe','pipe']});
 d.account=new VKAccount(d,d.emit);
 await d.search('BMW');const search=events.pop();assert(!search.error,JSON.stringify(search));assert(search.items.length>0);assert(search.items.every(x=>/^-?\d+_\d+$/.test(x.id)));
 await d.account.feed('home');const home=events.pop();assert(!home.error,JSON.stringify(home));assert(home.items.length>0);
 await d.account.favorite(search.items[0].id);assert(events.pop().items.length===1);
 await d.account.feed('subs');assert(events.pop().error);
 await d.account.start();assert(events.pop().state==='error');
 const value={search:search.items.length,catalog:home.items.length,local_favorites:true,login_not_advertised:true};
 console.log(JSON.stringify(value));
 const dest=process.env.GO_VIDEO_WEB_RESULT;if(dest)fs.writeFileSync(dest,JSON.stringify(value,null,2));
}
main().catch(e=>{console.error(e);process.exitCode=1;});
