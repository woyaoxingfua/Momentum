from typing import TYPE_CHECKING
from agents import function_tool

if TYPE_CHECKING:
    from ...storage import TaskStore


def create_relation_tools(store: 'TaskStore', user_id: str):
    from ...models import TaskRelationType
    from ._common import _to_json

    @function_tool
    def add_task_dependency(task_id: int, depends_on_task_id: int) -> str:
        """添加任务依赖：任务 task_id 依赖任务 depends_on_task_id 完成。"""
        relation = store.add_dependency(task_id, depends_on_task_id, user_id=user_id)
        if not relation:
            return f"无法创建依赖关系，请检查任务 #{task_id} 和 #{depends_on_task_id} 是否存在"
        return f"已创建依赖关系：任务 #{task_id} 依赖任务 #{depends_on_task_id}"

    @function_tool
    def remove_task_dependency(task_id: int, depends_on_task_id: int) -> str:
        """移除任务依赖。"""
        success = store.remove_dependency(task_id, depends_on_task_id, user_id=user_id)
        if not success:
            return f"未找到该依赖关系"
        return f"已移除依赖关系：任务 #{task_id} 不再依赖任务 #{depends_on_task_id}"

    @function_tool
    def get_task_dependencies(task_id: int) -> str:
        """获取任务的依赖列表（该任务依赖哪些任务完成）。"""
        dependencies = store.get_dependencies(task_id, user_id=user_id)
        return _to_json([
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority.value,
                "due_at": t.due_at.isoformat() if t.due_at else None
            }
            for t in dependencies
        ])

    @function_tool
    def get_task_dependents(task_id: int) -> str:
        """获取依赖该任务的任务列表（哪些任务在等这个任务完成）。"""
        dependents = store.get_dependents(task_id, user_id=user_id)
        return _to_json([
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority.value,
                "due_at": t.due_at.isoformat() if t.due_at else None
            }
            for t in dependents
        ])

    @function_tool
    def add_task_relation(source_task_id: int, target_task_id: int, relation_type: str = "relates_to") -> str:
        """添加任务关系。

        relation_type 可选：depends_on, blocks, relates_to, follows, parent_of
        """
        try:
            rel_type = TaskRelationType(relation_type)
        except ValueError:
            return f"无效的关系类型，请使用：depends_on, blocks, relates_to, follows, parent_of"

        relation = store.add_task_relation(source_task_id, target_task_id, rel_type, user_id=user_id)
        if not relation:
            return f"无法创建关系，请检查任务是否存在"
        return f"已创建关系：任务 #{source_task_id} {relation_type} 任务 #{target_task_id}"

    @function_tool
    def get_task_relations(task_id: int) -> str:
        """获取任务的所有关系。"""
        relations = store.get_task_relations(task_id, user_id=user_id)
        return _to_json([
            {
                "id": r.id,
                "source_task_id": r.source_task_id,
                "target_task_id": r.target_task_id,
                "relation_type": r.relation_type.value,
                "created_at": r.created_at.isoformat()
            }
            for r in relations
        ])

    @function_tool
    def is_task_blocked(task_id: int) -> str:
        """检查任务是否被依赖阻塞（有未完成的前置依赖）。"""
        return _to_json({"blocked": store.is_task_blocked(task_id, user_id=user_id)})

    return [
        add_task_dependency,
        remove_task_dependency,
        get_task_dependencies,
        get_task_dependents,
        add_task_relation,
        get_task_relations,
        is_task_blocked,
    ]
