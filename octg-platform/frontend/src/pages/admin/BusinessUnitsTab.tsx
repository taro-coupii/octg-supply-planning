import { useEffect, useState } from "react";
import ConfirmButton from "../../components/ConfirmButton";
import { ApiError, apiGet, apiSend } from "../../lib/api";
import { flattenBuTree, type BusinessUnitNode, type FlatBu } from "./shared";

export default function BusinessUnitsTab() {
  const [flat, setFlat] = useState<FlatBu[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newParent, setNewParent] = useState("");

  const load = () => {
    apiGet<BusinessUnitNode[]>("/business-units")
      .then((tree) => {
        setFlat(flattenBuTree(tree));
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, []);

  const create = () => {
    if (!newName.trim()) return;
    apiSend("POST", "/admin/business-units", { name: newName.trim(), parent_id: newParent || null })
      .then(() => {
        setNewName("");
        setNewParent("");
        load();
      })
      .catch((e: ApiError | Error) => setError(e.message));
  };

  const rename = (id: string, name: string) => {
    apiSend("PATCH", `/admin/business-units/${id}`, { name })
      .then(load)
      .catch((e: ApiError | Error) => setError(e.message));
  };

  const reparent = (id: string, parentId: string) => {
    apiSend("PATCH", `/admin/business-units/${id}`, { parent_id: parentId || null })
      .then(load)
      .catch((e: ApiError | Error) => setError(e.message));
  };

  const remove = (id: string) => {
    apiSend("DELETE", `/admin/business-units/${id}`)
      .then(load)
      .catch((e: ApiError | Error) => setError(e.message));
  };

  if (flat === null && !error) return <p>Loading…</p>;

  return (
    <div>
      {error && <p className="inline-error">{error}</p>}
      {flat && (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Parent</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {flat.map((bu) => (
              <tr key={bu.id}>
                <td style={{ paddingLeft: `${bu.depth * 20}px` }}>
                  <input
                    defaultValue={bu.name}
                    onBlur={(e) => {
                      if (e.target.value !== bu.name) rename(bu.id, e.target.value);
                    }}
                  />
                </td>
                <td>
                  <select
                    defaultValue={bu.parent_id ?? ""}
                    onChange={(e) => reparent(bu.id, e.target.value)}
                  >
                    <option value="">(root)</option>
                    {flat
                      .filter((o) => o.id !== bu.id)
                      .map((o) => (
                        <option key={o.id} value={o.id}>
                          {" ".repeat(o.depth * 2)}
                          {o.name}
                        </option>
                      ))}
                  </select>
                </td>
                <td>
                  <ConfirmButton label="Delete" armedLabel="Confirm?" onConfirm={() => remove(bu.id)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="admin-add-row">
        <input placeholder="New business unit name" value={newName} onChange={(e) => setNewName(e.target.value)} />
        <select value={newParent} onChange={(e) => setNewParent(e.target.value)}>
          <option value="">(root)</option>
          {(flat ?? []).map((o) => (
            <option key={o.id} value={o.id}>
              {o.name}
            </option>
          ))}
        </select>
        <button type="button" onClick={create}>
          Add
        </button>
      </div>
    </div>
  );
}
