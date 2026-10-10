"""Experimental, read-only battle source; never attaches using stale saved addresses."""
import json
import struct
import time
from pathlib import Path

from .events import ActionLedger


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
        raw = r.read(enemy + 72, 16)
        if len(raw) != 16:
            raise RuntimeError('资源读取中断，已停止，保留最后有效值')
        hp, dp = struct.unpack('<qq', raw)
        if min(hp, dp) < 0 or max(hp, dp) > 10**12:
            raise RuntimeError('资源值越界，已停止读取')
        states.append(dict(address=enemy, hp=hp, dp=dp, name=r.string(r.q(enemy + 496)) or f'敌人 {len(states)+1}'))
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
                if r.cls(content) != 'HitContent':
                    raise RuntimeError('命中内容已失效')
                target = r.q(hit + 24)
                target_name = r.string(r.q(r.q(r.q(target + 488) + 16) + 24)) if r.cls(target) == 'BattlePlayerData' else r.string(r.q(target + 496)) if r.cls(target) == 'BattleEnemyData' else None
                hits.append(dict(address=hit, target=target, target_name=target_name, type=r.i(hit + 16),
                    finished=r.read(hit + 41, 1) == b'\1', damage=r.i(content + 36) or 0,
                    raw_damage=r.i(content + 32), result_type=r.i(content + 28),
                    index=r.i(hit + 20), funnel=r.read(content + 174, 1) == b'\1',
                    critical=r.read(content + 171, 1) == b'\1'))
        actions.append(dict(address=entry, actor=actor, actor_is_player=r.cls(actor) == 'BattlePlayerData', executing=entry == preserved, actor_name=r.string(r.q(card + 24)) or r.cls(actor),
            skill=r.string(r.q(master + 24)), skill_name=r.string(r.q(master + 32)) or '未知技能',
            hit_set=hit_set, hits=hits))
    return states, actions


def run(stop, paused, emit, outdir):
    from . import runtime as r
    from .windows import game_pid
    from .names import Names
    import uuid
    r.STOP = stop
    run_id = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    ledger = ActionLedger()
    names = Names()
    try:
        r.open_process(game_pid())
        emit(('status', '正在定位战斗，请暂不行动…'))
        module = locate(r)
        states, actions = snapshot(r, module)
        if actions:
            raise RuntimeError('正在行动中，请等指令选择阶段再连接。')
        path = Path(outdir) / (run_id + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = None
        known_enemies = {s['address'] for s in states}
        heartbeat = 0.0
        with path.open('x', encoding='utf-8') as log:
            log.write(json.dumps({'schema': 1, 'run_id': run_id, 'kind': 'start', 'time': time.time()})+'\n')
            log.flush()
            emit(('connected', {'run_id': run_id, 'path': str(path)}))
            while not stop.is_set():
                if paused.is_set():
                    stop.wait(0.1)
                    continue
                states, actions = snapshot(r, module)
                if not states:
                    break
                known_enemies.update(s['address'] for s in states)
                for action in actions:
                    action['actor_name'] = names.actor(action['actor_name'])
                    for hit in action['hits']:
                        if hit.get('target_name'):
                            hit['target_name'] = names.actor(hit['target_name'])
                events = ledger.update(actions, known_enemies)
                if states != previous or events:
                    packet = dict(time=time.time(), enemies=states, events=events)
                    emit(('snapshot', packet))
                    log.write(json.dumps(packet, ensure_ascii=False)+'\n')
                    log.flush()
                    previous = states
                if time.monotonic() - heartbeat >= 1:
                    emit(('heartbeat', time.time()))
                    heartbeat = time.monotonic()
                if all(s['hp'] == 0 for s in states) and not actions:
                    break
                stop.wait(0.02)
            log.write(json.dumps({'kind': 'end', 'time': time.time(), 'reason': 'stopped' if stop.is_set() else 'battle_end'})+'\n')
        emit(('status', '已停止 · 记录已保存' if stop.is_set() else '战斗结束 · 记录已保存'))
    except InterruptedError:
        emit(('status', '已取消连接'))
    except Exception as exc:
        emit(('error', str(exc)))
    finally:
        if r.h:
            r.close(r.h)
            r.h = None
        emit(('stopped', None))
