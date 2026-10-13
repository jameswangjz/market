export function flattenSyncTree(tree, kind, mapped = false) {
  const rows = [];
  if (mapped) {
    const byId = new Map((tree?.mapped_departments || []).map(row => [row.id, row]));
    const depth = row => {
      let value = 0;
      let parent = byId.get(row.parent_mapping_id);
      const visited = new Set([row.id]);
      while (parent && !visited.has(parent.id)) { visited.add(parent.id); value += 1; parent = byId.get(parent.parent_mapping_id); }
      return value;
    };
    const visit = (parent, level, visited = new Set()) => {
      for (const row of byId.values()) {
        if ((row.parent_mapping_id || "") !== parent || visited.has(row.id)) continue;
        visited.add(row.id);
        rows.push({ ...row, type: "department", selectable: kind === "departments", depth: level, key: `mapping-department:${row.id}` });
        if (kind === "users") {
          for (const user of tree?.mapped_users || []) {
            if ((user.department_ids || []).includes(row.external_department_id) || user.department_id === row.platform_department_id)
              rows.push({ ...user, type: "user", selectable: true, depth: level + 1, key: `mapping-user:${row.id}:${user.id}` });
          }
        }
        visit(row.id, level + 1, visited);
      }
      return visited;
    };
    const visited = visit("", 0);
    for (const row of byId.values()) if (!visited.has(row.id)) visit(row.parent_mapping_id || "", depth(row), visited);
    if (kind === "users") for (const user of tree?.mapped_users || []) {
      if (!rows.some(row => row.type === "user" && row.id === user.id)) rows.push({ ...user, type: "user", selectable: true, depth: 0, key: `mapping-user:unassigned:${user.id}` });
    }
    return rows;
  }
  const visit = (departments, depth = 0) => {
    for (const department of departments || []) {
      const departmentRow = { id: department.id, name: department.name, depth, mapped: Boolean(department.mapped), type: "department", selectable: kind === "departments", key: `department:${department.id}` };
      if (kind === "departments" && !departmentRow.mapped) rows.push(departmentRow);
      if (kind === "users") {
        rows.push(departmentRow);
        for (const user of department.users || []) {
          rows.push({ ...user, depth: depth + 1, type: "user", selectable: true, key: `user:${department.id}:${user.id}` });
        }
      }
      visit(department.children, depth + 1);
    }
  };
  visit(tree?.departments);
  if (kind === "users") {
    for (const user of tree?.unassigned_users || []) {
      rows.push({ ...user, depth: 0, type: "user", selectable: true, key: `user:unassigned:${user.id}` });
    }
  }
  return rows;
}

export function selectedSyncIds(rows, kind, ids) {
  const allowed = new Set(rows.filter(row => row.selectable && row.type === (kind === "users" ? "user" : "department")).map(row => row.id));
  return [...new Set(ids)].filter(id => allowed.has(id));
}

export function syncActionPayload(action, kind, ids = []) {
  if (!ids.length) throw new Error("请选择同步项目");
  return { action, kind, ids: [...new Set(ids)] };
}

export function syncActionBatches(rows, action, kind, ids = null) {
  const selected = new Set(selectedSyncIds(rows, kind, ids === null ? rows.map(row => row.id) : ids));
  const ordered = rows.filter(row => selected.has(row.id) && row.selectable &&
    row.type === (kind === "users" ? "user" : "department"));
  if (kind === "departments") ordered.sort((a, b) => action === "add" ? a.depth - b.depth : b.depth - a.depth);
  const unique = [...new Set(ordered.map(row => row.id))];
  if (!unique.length) throw new Error("请选择同步项目");
  const batches = [];
  for (let offset = 0; offset < unique.length; offset += 10) batches.push(unique.slice(offset, offset + 10));
  return batches;
}
