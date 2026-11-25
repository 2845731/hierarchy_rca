def replay_evidence(answer, parquet_store, zero_evidence_score=0.0):
    records = []
    for collection in ("root_causes", "propagation"):
        for item_index, item in enumerate(answer[collection]):
            for evidence_index, evidence in enumerate(item["evidence"]):
                record = {"collection": collection, "item_index": item_index,
                          "evidence_index": evidence_index, **evidence}
                try:
                    result = parquet_store.query(evidence["sql"], evidence_only=True)
                    record["status"] = "OK" if result["has_rows"] else "EMPTY"
                    record["sample"] = result
                except Exception as error:
                    record.update(status="SQL_ERROR", error=str(error), error_type=type(error).__name__)
                records.append(record)
    return {"items": records, "sql_exec": sum(r["status"] == "OK" for r in records) / len(records) if records else zero_evidence_score,
            "evidence_support": "not_scored; executability does not establish the claim"}
