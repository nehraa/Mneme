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
collecting ... collected 16 items

tests/test_memory_store.py::TestHealth::test_health PASSED               [  6%]
tests/test_memory_store.py::TestCreateMemory::test_create_chunk_returns_mock_with_correct_schema PASSED [ 12%]
tests/test_memory_store.py::TestCreateMemory::test_create_chunk_all_outcome_tags PASSED [ 18%]
tests/test_memory_store.py::TestGetMemory::test_get_chunk_not_found PASSED [ 25%]
tests/test_memory_store.py::TestGetMemory::test_get_chunk_updates_last_accessed PASSED [ 31%]
tests/test_memory_store.py::TestUpdateTags::test_update_tags_success PASSED [ 37%]
tests/test_memory_store.py::TestUpdateTags::test_update_tags_not_found PASSED [ 43%]
tests/test_memory_store.py::TestListMemories::test_list_all PASSED       [ 50%]
tests/test_memory_store.py::TestListMemories::test_list_filter_by_tag PASSED [ 56%]
tests/test_memory_store.py::TestListMemories::test_list_filter_by_outcome PASSED [ 62%]
tests/test_memory_store.py::TestListMemories::test_list_filter_by_session PASSED [ 68%]
tests/test_memory_store.py::TestIngestStub::test_ingest_returns_mock_manifest PASSED [ 75%]
tests/test_memory_store.py::TestGraphStub::test_get_related_returns_mock PASSED [ 81%]
tests/test_memory_store.py::TestRetrieveStub::test_retrieve_returns_mock_injection PASSED [ 87%]
tests/test_memory_store.py::TestGuardStub::test_guard_returns_mock_warning PASSED [ 93%]
tests/test_memory_store.py::TestInjectStub::test_inject_returns_full_mock PASSED [100%]

============================== 16 passed in 0.39s ==============================
```
