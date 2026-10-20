/* 全产品金额展示：整数分计算、千分位与中文大写。 */
window.NavrenMoneyInput = (() => {
function format(value) {
    const normalized=String(value).trim().replace(/[０-９]/g,c=>String(c.charCodeAt(0)-0xff10)).replace(/[，]/g,',').replace(/[．]/g,'.');
    if(!normalized)return {raw:'',formatted:'',minor:null,uppercase:''};
    const raw=normalized.replaceAll(',','');
    if(!/^\d*(?:\.\d{0,2})?$/.test(raw)||!/[0-9]/.test(raw))return {formatted:normalized,minor:null,error:'请填写有效金额，最多两位小数。'};
    let [yuan,cents]=raw.split('.');yuan=(yuan||'0').replace(/^0+(?=\d)/,'');
    if(yuan.length>12)return {formatted:normalized,minor:null,error:'金额超出可填写范围。'};
    const minor=BigInt(yuan)*100n+BigInt((cents||'').padEnd(2,'0'));
    const digits='零壹贰叁肆伍陆柒捌玖',units=['','拾','佰','仟'],large=['','万','亿'];
    function group(value) {
      let result='',zero=false;
      const chars=String(value).padStart(4,'0');
      for(let i=0;i<4;i++){const digit=Number(chars[i]);if(digit){if(zero&&result)result+='零';result+=digits[digit]+units[3-i];zero=false;}else if(result)zero=true;}
      return result;
    }
    const groups=[];let integer=BigInt(yuan);
    while(integer){groups.push(Number(integer%10000n));integer/=10000n;}
    let upper='',gap=false;
    for(let i=groups.length-1;i>=0;i--){const part=groups[i];if(!part){if(upper)gap=true;continue;}if(upper&&(gap||part<1000))upper+='零';upper+=group(part)+large[i];gap=false;}
    upper=(upper||'零')+'元';
    const fraction=Number(minor%100n),jiao=Math.floor(fraction/10),fen=fraction%10;
    if(!fraction)upper+='整';else {if(jiao)upper+=digits[jiao]+'角';else if(minor>=100n)upper+='零';if(fen)upper+=digits[fen]+'分';else upper+='整';}
    return {raw:yuan+(cents!==undefined?'.'+cents:''),formatted:yuan.replace(/\B(?=(\d{3})+(?!\d))/g,',')+(cents!==undefined?'.'+cents:''),minor:Number(minor),uppercase:upper};
  }
  function formatInput(input) {
    const cursor=input.selectionStart, prefix=cursor==null?null:input.value.slice(0,cursor).replaceAll(',','').length;
    const result=format(input.value);
    if(!result.error){input.value=result.formatted;if(prefix!=null&&typeof input.setSelectionRange==='function'){let position=0,count=0;while(position<input.value.length&&count<prefix){if(input.value[position]!==',')count++;position++;}input.setSelectionRange(position,position);}}
    return result;
  }
  return Object.freeze({format,formatInput});
})();
