(function() {
  function wireFilter(selectId, gridId) {
    var sel = document.getElementById(selectId);
    var grid = document.getElementById(gridId);
    if (!sel || !grid) return;

    var items = Array.prototype.slice.call(grid.querySelectorAll('.portfolio-item'));

    function apply(area) {
      area = (area || 'all').toLowerCase();
      items.forEach(function(el){
        var a = (el.getAttribute('data-area') || '').toLowerCase();
        el.style.display = (area === 'all' || a === area) ? '' : 'none';
      });
    }

    sel.addEventListener('change', function(){
      apply(this.value);
    });

    apply(sel.value);
  }

  wireFilter('areaFilter-und',  'grid-und');
  wireFilter('areaFilter-grad', 'grid-grad');
})();
