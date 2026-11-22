'use strict';
const create = require('./qrcode-generator');
function encode(text) {
    const qr = create(0,'M'); qr.addData(String(text),'Byte'); qr.make();
    const n = qr.getModuleCount(), rows = [];
    for(let y=0;y<n;y++) { const row=[]; for(let x=0;x<n;x++) row.push(qr.isDark(y,x)); rows.push(row); }
    return rows;
}
function draw(cv,text,x,y,mod) {
    const rows=encode(text), n=rows.length, side=(n+8)*mod;
    cv.fillRect(x,y,side,side,'#ffffff');
    rows.forEach((row,iy)=>row.forEach((dark,ix)=>{if(dark)cv.fillRect(x+(ix+4)*mod,y+(iy+4)*mod,mod,mod,'#000000');}));
    return side;
}
module.exports={encode,draw};
