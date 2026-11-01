import pytest
from search_console_mcp.client import validate_inspection_target
from search_console_mcp.errors import InvalidArgumentError

@pytest.mark.parametrize('target', [
    'https://www.example.com/shop/%252e%252e/admin',
    'https://www.example.com/shop/%252e%252e%252fadmin',
    'https://www.example.com/shop/%2e%2e%5cadmin',
])
def test_encoded_nested_path_traversal_is_rejected(target):
    with pytest.raises(InvalidArgumentError):
        validate_inspection_target('https://www.example.com/shop/', target)
