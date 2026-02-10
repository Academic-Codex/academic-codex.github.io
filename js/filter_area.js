(function () {
  // Função que normaliza strings (remove acentos, espaços e pontuação)
  function norm(s){
    return (s || "")
      .toString()
      .normalize("NFD")               // separa acentos em caracteres combinantes
      .replace(/[\u0300-\u036f]/g,"") // remove os acentos
      .trim()
      .toLowerCase()
      .replace(/[^\w]+/g,"-")         // espaços/pontuação -> '-'
      .replace(/-+/g,"-");            // remove hífens duplicados
  }

  function wireFilter(selectId, gridId) {
    const sel  = document.getElementById(selectId);
    const grid = document.getElementById(gridId);
    if (!sel || !grid) return;

    const items = Array.from(grid.querySelectorAll('.portfolio-item'));

    function apply(val) {
      const want = norm(val);
      items.forEach(el => {
        const tag = norm(el.dataset.area || ""); // normaliza também o atributo
        el.style.display = (want === "all" || tag === want) ? "" : "none";
      });
    }

    sel.addEventListener('change', () => apply(sel.value));
    apply(sel.value);
  }

  wireFilter('areaFilter-und',  'grid-und');
  wireFilter('areaFilter-grad', 'grid-grad');
})();