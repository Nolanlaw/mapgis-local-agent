"""Exercise native object parsing at the process-memory read boundary."""
import struct

import pytest

from mapgis_agent.native_state import NativeStateError, read_project_view, read_workspaces


class Memory:
    def __init__(self, selection=((5, 2, 26), (7, 2, 26), (5, 4, 12))):
        self.blocks = {}
        view = bytearray(0x649)
        struct.pack_into('<iI', view, 0x175, len(selection), 0x6000)
        struct.pack_into('<I', view, 0x645, 0x2000)
        self.blocks[0x1000] = view
        project = bytearray(0xB37)
        name = 'E:\\work\\project.MPJ'.encode('gbk')
        project[0x3E0:0x3E0 + len(name)] = name
        project[0x5E4:0x5EF] = b'project.MPJ'
        struct.pack_into('<h', project, 0xA, 1)
        struct.pack_into('<h', project, 0x6E6, 2)
        struct.pack_into('<I', project, 0xB33, 0x3000)
        self.blocks[0x2000] = project
        for address, area, path, following in [
            (0x3000, 5, 'E:\\work\\first.WP', 0x4000),
            (0x4000, 7, 'E:\\work\\second.WP', 0),
        ]:
            node = bytearray(0x19A)
            node[4:6] = bytes((2, 2))
            encoded = path.encode('gbk')
            node[6:6 + len(encoded)] = encoded
            struct.pack_into('<hI', node, 0x194, area, following)
            self.blocks[address] = node
        self.blocks[0x6000] = b''.join(struct.pack('<hhi', *r) for r in selection)

    def __call__(self, address, size):
        for start, data in self.blocks.items():
            if start <= address and address + size <= start + len(data):
                return bytes(data[address - start:address - start + size])
        raise NativeStateError('Unmapped read in fixture')


def test_multiselection_resolves_workspace_and_entity_not_record_number_alone():
    state = read_project_view(Memory(), 0x1000)
    assert state['project_path'] == 'E:\\work\\project.MPJ'
    assert state['project_modified'] is True
    assert state['selection_count'] == 3
    assert [row['layer_path'] for row in state['selection']] == [
        'E:\\work\\first.WP', 'E:\\work\\second.WP', 'E:\\work\\first.WP']
    assert state['selection'][2]['entity'] == 'ARC'
    assert state['layers'][0]['native_state'] == 2
    assert 'editable' not in state['layers'][0]


def test_verified_empty_selection_is_distinct_from_unknown_ui_selection():
    state = read_project_view(Memory(()), 0x1000)
    assert state['selection_count'] == 0
    assert state['selection'] == []
    assert state['workspace_modified'] is None


def test_same_workspace_for_two_layers_does_not_guess_selection_owner():
    memory = Memory(((5, 2, 26),))
    struct.pack_into('<h', memory.blocks[0x4000], 0x194, 5)
    state = read_project_view(memory, 0x1000)
    assert state['selection'][0]['layer_path'] is None
    assert state['selection'][0]['layer_binding_status'] == 'ambiguous'


@pytest.mark.parametrize('cycle', [True, False])
def test_inconsistent_project_item_chain_is_rejected(cycle):
    memory = Memory()
    struct.pack_into('<I', memory.blocks[0x4000], 0x196, 0x3000 if cycle else 0x5000)
    with pytest.raises(NativeStateError, match='chain|count'):
        read_project_view(memory, 0x1000)


def test_selection_change_during_read_is_rejected():
    memory = Memory()
    calls = 0

    def moving(address, size):
        nonlocal calls
        if address == 0x6000:
            calls += 1
            if calls == 2:
                return struct.pack('<hhi', 5, 2, 27) + memory(address, size)[8:]
        return memory(address, size)

    with pytest.raises(NativeStateError, match='changed during'):
        read_project_view(moving, 0x1000)


def test_unknown_entity_cannot_be_published_as_a_valid_selection():
    with pytest.raises(NativeStateError, match='selection'):
        read_project_view(Memory(((5, 9, 26),)), 0x1000)


def test_workspace_modified_is_bit_zero_not_any_nonzero_flag():
    memory = Memory()
    memory.blocks[0xA000 + 0x8D928] = struct.pack('<I', 0x7000)
    for address, area, flags, following in [(0x7000, 5, 0x20, 0x8000), (0x8000, 7, 0x21, 0)]:
        node = bytearray(0xEB8)
        struct.pack_into('<h', node, 0, area)
        struct.pack_into('<I', node, 8, following)
        node[0xEB7] = flags
        memory.blocks[address] = node
    state = read_workspaces(memory, 0xA000)
    assert state[5]['modified'] is False
    assert state[7]['modified'] is True


def test_workspace_counts_distinguish_deleted_slots_and_polygon_arcs():
    memory = Memory()
    memory.blocks[0xA000 + 0x8D928] = struct.pack('<I', 0x7000)
    for address, area, kind, following in [(0x7000, 5, 1, 0x9000), (0x9000, 7, 2, 0)]:
        node = bytearray(0xEB8)
        struct.pack_into('<h', node, 0, area)
        struct.pack_into('<I', node, 8, following)
        node[0xACD] = kind
        struct.pack_into('<ii', node, 0xAD2, 2 if kind == 1 else 12, 5 if kind == 1 else 17)
        struct.pack_into('<ii', node, 0xAE2, 3, 5)
        memory.blocks[address] = node
    state = read_workspaces(memory, 0xA000)
    assert state[5]['counts'] == {'POINT': {'active': 2, 'max_physical_id': 4}}
    assert state[7]['counts'] == {'ARC': {'active': 12, 'max_physical_id': 16},
                                'REGION': {'active': 3, 'max_physical_id': 4}}


def test_workspace_counts_resolve_alias_and_do_not_guess_external_storage():
    memory = Memory()
    memory.blocks[0xA000 + 0x8D928] = struct.pack('<I', 0x7000)
    for address, area, following in [(0x7000, 5, 0x9000), (0x9000, 7, 0)]:
        node = bytearray(0xEB8)
        struct.pack_into('<h', node, 0, area)
        struct.pack_into('<I', node, 8, following)
        node[0xACD] = 1
        struct.pack_into('<ii', node, 0xAD2, 2, 5)
        memory.blocks[address] = node
    struct.pack_into('<ih', memory.blocks[0x7000], 0xC, 1, 7)
    state = read_workspaces(memory, 0xA000)
    assert state[5]['counts'] == state[7]['counts']
    assert state[5]['count_source_area'] == 7
    struct.pack_into('<I', memory.blocks[0x9000], 0x143, 1)
    state = read_workspaces(memory, 0xA000)
    assert state[5]['counts'] is None and state[5]['counts_status'] == 'EXTERNAL_STORAGE'
