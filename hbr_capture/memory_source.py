"""Experimental, read-only battle source; never attaches using stale saved addresses."""
import json
import struct
import time
from pathlib import Path

from .memory_events import ActionLedger


def members(r, address):
    size = r.i(address + 24)
    if size is None or not 0 <= size <= 256:
        raise RuntimeError('列表布局不匹配或战斗对象已失效')
    array = r.q(address + 16)
    return [r.q(array + 32 + j * 8) for j in range(size)]


def locate(r):
    """Find class metadata then instances; reject ambiguous retained battles."""
    names, _ = r.scan(b'BattleModule\0')
    if not names:
        raise RuntimeError('未找到战斗运行时，请进入战斗后重试')
    refs, _ = r.scan(tuple(struct.pack('<Q', n) for n in names), seconds=60)
    classes = {ref - 16 for ref in refs if r.name(r.q(ref)) == 'BattleModule'
               and r.name(r.q(r.q(ref - 16 + 88) + 16))}
    if not classes:
        raise RuntimeError('未找到 BattleModule 类，游戏版本可能不兼容')
    refs, _ = r.scan(tuple(struct.pack('<Q', k) for k in classes), seconds=60)
    candidates = []
    for obj in refs:
        parent = r.q(obj + 16)
        manager = r.q(obj + 144)
        if r.cls(parent) != 'BattleState' or r.q(parent + 360) != obj:
            continue
        if r.cls(manager) != 'BattleCharacterDataManager':
            continue
        try:
            enemies = members(r, r.q(manager + 168))
            if enemies and all(r.cls(e) == 'BattleEnemyData' for e in enemies):
                if any(r.q(e + 72) > 0 for e in enemies):
                    candidates.append(obj)
        except RuntimeError:
            continue
    candidates = list(set(candidates))
    if len(candidates) != 1:
        raise RuntimeError(f'找到 {len(candidates)} 个可能的战斗对象，无法安全选择。请进入战斗后重试；若仍有多个，请重启游戏。')
    obj = candidates[0]
    required = {64: 'BattleInfoManager', 96: 'BattleActionEntryManager',
                144: 'BattleCharacterDataManager', 216: 'BattleTimelineManager'}
    if any(r.cls(r.q(obj + offset)) != name for offset, name in required.items()):
        raise RuntimeError('游戏内存布局不兼容，此版本暂不可读取')
    return obj


def snapshot(r, module):
    manager = r.q(module + 144)
    enemies = members(r, r.q(manager + 168))
    states = []
    for enemy in enemies:
        if r.cls(enemy) != 'BattleEnemyData':
            raise RuntimeError('敌方对象失效，请重新连接')
        hp, dp = r.q(enemy + 72), r.q(enemy + 80)
        if max(hp, dp) > 10**12:
            raise RuntimeError('资源值越界，已停止读取')
        states.append(dict(address=enemy, hp=hp, dp=dp))
    action_manager = r.q(module + 96)
    entries = members(r, r.q(action_manager + 16))
    preserved = r.q(action_manager + 40)
    if preserved and preserved not in entries:
        entries.append(preserved)
    actions = []
    for entry in entries:
        if r.cls(entry) != 'ActionEntry':
            raise RuntimeError('行动对象布局不匹配')
        actor = r.q(entry + 32)
        skill = r.q(entry + 24)
        master = r.q(r.q(skill + 16) + 16)
        card = r.q(r.q(actor + 488) + 16) if r.cls(actor) == 'BattlePlayerData' else 0
        hit_set = r.q(entry + 40)
        hits = []
        if hit_set:
            for hit in members(r, r.q(hit_set + 16)):
                if r.cls(hit) != 'HitInfo':
                    raise RuntimeError('命中对象布局不匹配')
                content = r.q(hit + 32)
                hits.append(dict(address=hit, target=r.q(hit + 24), type=r.i(hit + 16),
                    finished=r.read(hit + 41, 1) == b'\1', damage=r.i(content + 36) or 0,
                    funnel=r.read(content + 174, 1) == b'\1',
                    critical=r.read(content + 171, 1) == b'\1'))
        actions.append(dict(address=entry, actor=actor, actor_name=r.string(r.q(card + 24)) or r.cls(actor),
            skill=r.string(r.q(master + 24)), skill_name=r.string(r.q(master + 32)) or '未知技能',
            hit_set=hit_set, hits=hits))
    return states, actions


def run(pid, stop, emit, outdir):
    from . import memory_runtime as r
    r.STOP = stop
    run_id = time.strftime('%Y%m%d-%H%M%S') + '-memory'
    ledger = ActionLedger()
    try:
        r.open_process(pid)
        emit(('memory_status', '正在定位战斗，请暂不行动…'))
        module = locate(r)
        states, actions = snapshot(r, module)
        if actions:
            raise RuntimeError('正在行动中，请等指令选择阶段再连接，避免导入半次攻击')
        maxima = {s['address']: (s['dp'], s['hp']) for s in states}
        emit(('memory_status', '内存已连接 · 可以行动'))
        path = Path(outdir) / (run_id + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = None
        with path.open('a', encoding='utf-8') as log:
            while not stop.is_set():
                states, actions = snapshot(r, module)
                if not states:
                    break
                if any(s['address'] not in maxima for s in states):
                    raise RuntimeError('敌人列表已更换，请停止后重新连接')
                resources = dict(dp=sum(s['dp'] for s in states), hp=sum(s['hp'] for s in states),
                    dp_max=sum(x[0] for x in maxima.values()), hp_max=sum(x[1] for x in maxima.values()), enemies=states)
                events = ledger.update(actions, set(maxima))
                if resources != previous or events:
                    emit(('memory_resources', resources))
                    for event in events:
                        event['run'] = run_id
                        emit(('damage', event))
                    log.write(json.dumps(dict(time=time.time(), resources=resources, events=events), ensure_ascii=False)+'\n')
                    log.flush()
                    previous = resources
                if all(s['hp'] == 0 for s in states) and not actions:
                    break
                stop.wait(0.02)
        emit(('memory_status', '本次读取结束 · 结果已保存'))
    except InterruptedError:
        pass
    except Exception as exc:
        emit(('memory_status', str(exc)))
        emit(('log', '[内存读取] ' + str(exc)))
    finally:
        if r.h:
            r.close(r.h)
            r.h = None
        emit(('stopped',))
