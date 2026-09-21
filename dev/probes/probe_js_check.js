((s) => (s||'').trim(), ph, target) => {
    const inputs = [...document.querySelectorAll('.el-select input.el-input__inner')];
    const inp = inputs.find(i => (i.placeholder||'').trim() === ph);
    const drops = [...document.querySelectorAll('.el-select-dropdown')];
    const visible = drops.filter(d => {
        const r = d.getBoundingClientRect();
        return r.width>0 && r.height>0 && getComputedStyle(d).display!=='none';
    });
    const dd = visible[0] || drops[0];
    if (!dd) return 'no-dropdown';
    const items = [...dd.querySelectorAll('.el-select-dropdown__item')];
    let it = items.find(e => (s) => (s||'').trim()(e.textContent) === target);
    if (!it) it = items.find(e => (s) => (s||'').trim()(e.textContent).includes(target));
    if (it) { it.click(); return 'clicked'; }
    return 'waiting:' + items.length;
}