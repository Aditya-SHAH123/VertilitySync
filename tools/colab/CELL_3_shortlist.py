# ═══════════════════════════════════════════════════════════════════════════
#  CELL 3 of 5  —  CHOOSE WHICH SERIES TO MOVE   (replaces the earlier Cell 3)
#
#  Selects on ESTIMATED PEAK MEMORY, not slice count. Slice count alone is
#  misleading when a collection mixes matrix sizes: a 512-slice 768x768 study
#  needs 2.25x the memory of a 512-slice 512x512 study.
#
#  peak RAM ≈ rows × cols × slices × 4 bytes × 6.9
#  (the 6.9 factor is measured, not assumed - segmentation and meshing hold
#   several full-size intermediate arrays.)
#
#  max_ram_gb=3.0 is a safe default for a 16 GB machine. Raise it only if you
#  know you have headroom.
# ═══════════════════════════════════════════════════════════════════════════

def shortlist(inv, modality='CT', body_part=None, min_slices=150,
              max_ram_gb=3.0, limit=5, prefer_largest=True):
    rows = []
    for s in inv:
        if modality and str(s['modality']).upper() != modality.upper():
            continue
        bp = str(s['body_part']).upper()
        if body_part and body_part.upper() not in bp and bp not in ('', '?', 'NONE'):
            continue
        if s['n_files'] < min_slices:
            continue
        r, c = s.get('rows', 0), s.get('columns', 0)
        if not r or not c:
            continue
        vol_gb = r * c * s['n_files'] * 4 / 1e9
        peak_gb = vol_gb * 6.9
        if peak_gb > max_ram_gb:
            continue
        rows.append((peak_gb, vol_gb, s))

    if not rows:
        print(f'Nothing fits under {max_ram_gb} GB.')
        smallest = sorted(
            ((s.get('rows',0)*s.get('columns',0)*s['n_files']*4/1e9*6.9, s) for s in inv
             if s.get('rows') and s['n_files'] >= min_slices), key=lambda t: t[0])[:3]
        for peak, s in smallest:
            print(f"  smallest available: {s['n_files']} slices "
                  f"{s['rows']}x{s['columns']} -> {peak:.1f} GB")
        print(f'\nEither raise max_ram_gb, or lower min_slices.')
        return []

    # biggest study that still fits comfortably = most anatomy per import
    rows.sort(key=lambda t: -t[0] if prefer_largest else t[0])
    picked = [t for t in rows[:limit]]

    print(f'{len(picked)} of {len(rows)} eligible series selected '
          f'(≤ {max_ram_gb} GB peak RAM each):\n')
    total_mb = 0
    for i, (peak, vol, s) in enumerate(picked):
        total_mb += s['size_mb']
        print(f"  [{i}] {s['n_files']:>4} slices  {s['rows']}x{s['columns']}  "
              f"{s['size_mb']:>7.1f} MB on disk  ->  {vol*1000:>4.0f} MB volume, "
              f"~{peak:.1f} GB peak RAM")
    print(f'\nTotal to move: {total_mb:.0f} MB  (of {sum(x["size_mb"] for x in inv)/1000:.1f} GB)')
    print('Import them ONE AT A TIME - the app holds one study at a time.')
    return [t[2] for t in picked]


picks = shortlist(inv)
