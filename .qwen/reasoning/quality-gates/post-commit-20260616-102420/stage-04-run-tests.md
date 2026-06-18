# Stage 4: Run Existing Tests

**Status:** PASS

```
============================= test session starts ==============================
platform darwin -- Python 3.10.12, pytest-9.0.3, pluggy-1.6.0 -- /Users/abhinavnehra/.pyenv/versions/3.10.12/bin/python3.10
cachedir: .pytest_cache
hypothesis profile 'default'
rootdir: /Users/abhinavnehra/git/Mneme
configfile: pyproject.toml
testpaths: tests
plugins: anyio-4.12.0, langsmith-0.6.9, cov-4.1.0, hypothesis-6.151.9, asyncio-1.3.0, typeguard-4.4.4
asyncio: mode=auto, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 63 items

tests/test_graph.py::TestMockGraphIndex::test_get_related_returns_required_keys PASSED [  1%]
tests/test_graph.py::TestMockGraphIndex::test_get_related_relationships_have_required_fields PASSED [  3%]
tests/test_graph.py::TestMockGraphIndex::test_get_related_implementation_note_present PASSED [  4%]
tests/test_graph.py::TestMockGraphIndex::test_get_chains_returns_required_keys PASSED [  6%]
tests/test_graph.py::TestMockGraphIndex::test_get_chains_paths_are_lists_of_chunk_ids PASSED [  7%]
tests/test_graph.py::TestMockGraphIndex::test_get_chains_implementation_note_present PASSED [  9%]
tests/test_graph.py::TestGraphIndex::test_get_related_with_mock_returns_mock_result PASSED [ 11%]
tests/test_graph.py::TestGraphIndex::test_get_related_passes_chunk_id_to_mock PASSED [ 12%]
tests/test_graph.py::TestGraphIndex::test_get_chains_with_mock_returns_mock_result PASSED [ 14%]
tests/test_graph.py::TestGraphEndpoints::test_related_returns_200 PASSED [ 15%]
tests/test_graph.py::TestGraphEndpoints::test_related_returns_mock_manifest PASSED [ 17%]
tests/test_graph.py::TestGraphEndpoints::test_related_depth_param_passed PASSED [ 19%]
tests/test_graph.py::TestGraphEndpoints::test_related_relationships_have_required_fields PASSED [ 20%]
tests/test_graph.py::TestGraphEndpoints::test_chains_returns_200 PASSED  [ 22%]
tests/test_graph.py::TestGraphEndpoints::test_chains_returns_mock_manifest PASSED [ 23%]
tests/test_graph.py::TestGraphEndpoints::test_chains_depth_param_passed PASSED [ 25%]
tests/test_graph.py::TestGraphEndpoints::test_chains_paths_are_list_of_strings PASSED [ 26%]
tests/test_ingestion.py::TestMockIngestionPipeline::test_run_returns_manifest_with_required_keys PASSED [ 28%]
tests/test_ingestion.py::TestMockIngestionPipeline::test_run_session_id_passed_through PASSED [ 30%]
tests/test_ingestion.py::TestMockIngestionPipeline::test_run_chunks_are_list_of_dicts PASSED [ 31%]
tests/test_ingestion.py::TestMockIngestionPipeline::test_run_tag_tree_has_expected_outcome_tags PASSED [ 33%]
tests/test_ingestion.py::TestMockIngestionPipeline::test_run_implementation_note_present PASSED [ 34%]
tests/test_ingestion.py::TestIngestionPipeline::test_run_with_mock_returns_mock_result PASSED [ 36%]
tests/test_ingestion.py::TestIngestionPipeline::test_run_passes_session_id_to_mock PASSED [ 38%]
tests/test_ingestion.py::TestIngestEndpoint::test_ingest_returns_200 PASSED [ 39%]
tests/test_ingestion.py::TestIngestEndpoint::test_ingest_returns_mock_manifest PASSED [ 41%]
tests/test_ingestion.py::TestIngestEndpoint::test_ingest_returns_200_on_empty_file_paths PASSED [ 42%]
tests/test_ingestion.py::TestIngestEndpoint::test_ingest_session_id_from_first_file_path PASSED [ 44%]
tests/test_ingestion.py::TestIngestEndpoint::test_ingest_tag_tree_summary_shape PASSED [ 46%]
tests/test_memory_store.py::TestHealth::test_health PASSED               [ 47%]
tests/test_memory_store.py::TestCreateMemory::test_create_chunk_returns_mock_with_correct_schema PASSED [ 49%]
tests/test_memory_store.py::TestCreateMemory::test_create_chunk_all_outcome_tags PASSED [ 50%]
tests/test_memory_store.py::TestGetMemory::test_get_chunk_not_found PASSED [ 52%]
tests/test_memory_store.py::TestGetMemory::test_get_chunk_updates_last_accessed PASSED [ 53%]
tests/test_memory_store.py::TestUpdateTags::test_update_tags_success PASSED [ 55%]
tests/test_memory_store.py::TestUpdateTags::test_update_tags_not_found PASSED [ 57%]
tests/test_memory_store.py::TestListMemories::test_list_all PASSED       [ 58%]
tests/test_memory_store.py::TestListMemories::test_list_filter_by_tag PASSED [ 60%]
tests/test_memory_store.py::TestListMemories::test_list_filter_by_outcome PASSED [ 61%]
tests/test_memory_store.py::TestListMemories::test_list_filter_by_session PASSED [ 63%]
tests/test_memory_store.py::TestIngestStub::test_ingest_returns_mock_manifest PASSED [ 65%]
tests/test_memory_store.py::TestGraphStub::test_get_related_returns_mock PASSED [ 66%]
tests/test_memory_store.py::TestRetrieveStub::test_retrieve_returns_mock_injection PASSED [ 68%]
tests/test_memory_store.py::TestGuardStub::test_guard_returns_mock_warning PASSED [ 69%]
tests/test_memory_store.py::TestInjectStub::test_inject_returns_full_mock PASSED [ 71%]
tests/test_retrieval.py::TestIntentDetector::test_detect_returns_required_keys PASSED [ 73%]
tests/test_retrieval.py::TestIntentDetector::test_detect_auth_keyword_tags_as_auth PASSED [ 74%]
tests/test_retrieval.py::TestIntentDetector::test_detect_failed_keyword_tags_as_failed PASSED [ 76%]
tests/test_retrieval.py::TestIntentDetector::test_detect_retry_keyword_sets_retry_intent PASSED [ 77%]
tests/test_retrieval.py::TestIntentDetector::test_detect_continue_keyword_sets_continue_intent PASSED [ 79%]
tests/test_retrieval.py::TestIntentDetector::test_detect_implementation_note_present PASSED [ 80%]
tests/test_retrieval.py::TestMockRetrievalEngine::test_retrieve_returns_required_keys PASSED [ 82%]
tests/test_retrieval.py::TestMockRetrievalEngine::test_retrieve_injected_context_is_string PASSED [ 84%]
tests/test_retrieval.py::TestMockRetrievalEngine::test_retrieve_priority_scores_have_failed_chunk_first PASSED [ 85%]
tests/test_retrieval.py::TestMockRetrievalEngine::test_retrieve_tag_matches_has_exact_for_failed PASSED [ 87%]
tests/test_retrieval.py::TestRetrievalEngine::test_retrieve_with_mock_returns_mock_result PASSED [ 88%]
tests/test_retrieval.py::TestRetrievalEngine::test_retrieve_intent_detected PASSED [ 90%]
tests/test_retrieval.py::TestRetrieveEndpoint::test_retrieve_returns_200 PASSED [ 92%]
tests/test_retrieval.py::TestRetrieveEndpoint::test_retrieve_returns_mock_manifest PASSED [ 93%]
tests/test_retrieval.py::TestRetrieveEndpoint::test_retrieve_session_id_passed PASSED [ 95%]
tests/test_retrieval.py::TestRetrieveEndpoint::test_retrieve_injected_context_is_string PASSED [ 96%]
tests/test_retrieval.py::TestRetrieveEndpoint::test_retrieve_priority_scores_are_floats PASSED [ 98%]
tests/test_retrieval.py::TestRetrieveEndpoint::test_retrieve_tag_matches_has_outcome_field PASSED [100%]

============================== 63 passed in 0.54s ==============================
```
