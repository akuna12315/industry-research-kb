"""Fixed error messages and bounded SDK metadata; never persist model content."""
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart

TOOLS = {'inspect_industry', 'search_materials', 'search_evidence', 'list_review_due',
         'read_user_memory', 'search_web', 'crawl_public_sources', 'read_public_source',
         'add_information_source', 'archive_evidence', 'create_evidence_brief',
         'compose_research_page', 'plan_visual_page', 'add_visual_scene', 'finish_visual_page',
         'draw_research_chart', 'revise_visual_scene', 'final_result'}
FIELDS = {'page', 'chart', 'title', 'subtitle', 'summary', 'summary_references', 'regions',
          'tags', 'audience', 'coverage', 'gaps', 'blocks', 'kind', 'columns', 'items',
          'label', 'text', 'cells', 'references', 'nature', 'date', 'date_kind', 'value',
          'status', 'quote', 'metric', 'unit', 'geography', 'scope', 'basis', 'note',
          'axis', 'period', 'x', 'x_metric', 'x_unit', 'whole', 'total',
          'support_ref',
          'description', 'query', 'record', 'url', 'indices', 'plan', 'scene', 'scenes',
          'nodes', 'edges', 'source', 'target', 'relation', 'dimensions', 'dimension', 'node',
          'id', 'icon', 'group', 'detail', 'question', 'theme', 'layout', 'direction', 'emphasis'}
ERRORS = {'missing', 'extra_forbidden', 'literal_error', 'string_too_long', 'string_too_short',
          'list_too_long', 'list_too_short', 'json_invalid', 'value_error', 'int_type',
          'float_type', 'finite_number', 'model_type', 'list_type', 'string_type'}


def failure_diagnostics(exc, messages=(), page=False):
    diagnostic = {'exception': type(exc).__name__, 'stage': 'page_generation' if page else 'agent',
                  'code': 'agent_failure'}
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    retries = [p for m in messages for p in m.parts if isinstance(p, RetryPromptPart)]
    truncated = [m for m in responses if m.finish_reason == 'length']
    if truncated:
        diagnostic['truncated_responses'] = len(truncated)
        diagnostic['truncated_output_tokens'] = truncated[-1].usage.output_tokens
    if responses:
        last = responses[-1]
        reason = last.finish_reason
        diagnostic['finish_reason'] = reason if reason in ('stop', 'length', 'tool_call', 'content_filter', 'error') else 'unknown'
        diagnostic['output_tokens'] = last.usage.output_tokens
        diagnostic['response_count'] = len(responses)
        diagnostic['last_tools'] = [p.tool_name if p.tool_name in TOOLS else 'unknown_tool'
                                    for p in last.parts if isinstance(p, ToolCallPart)][:6]
    if retries:
        retry = retries[-1]
        diagnostic['retry_count'] = len(retries)
        diagnostic['retry_tool'] = retry.tool_name if retry.tool_name in TOOLS else 'unknown_tool'
        if isinstance(retry.content, list):
            diagnostic['validation_errors'] = [
                {'type': e.get('type') if e.get('type') in ERRORS else 'validation_error',
                 'path': [str(k) if type(k) is int else k if k in FIELDS else 'field'
                          for k in e.get('loc', ())[:8]]}
                for e in retry.content[:6]]
        else:
            # Validation text can include user data. Categorize without retaining it.
            text = str(retry.content)
            diagnostic['validation_kind'] = next((code for words, code in (
                (('chart_ref', '引用', 'references'), 'citation'),
                (('日期', 'date'), 'date'), (('数值', '口径'), 'metric'),
                (('表格', '单元格'), 'table'), (('原文', '逐字'), 'source_support'))
                if any(word in text for word in words)), 'tool_validation')
    message = getattr(exc, 'message', '') if isinstance(exc, UnexpectedModelBehavior) else ''
    incomplete_page = page and isinstance(exc, ValueError) and bool(truncated) and bool(retries)
    if diagnostic.get('finish_reason') == 'length' or 'Model token limit' in message or incomplete_page:
        diagnostic['code'] = 'output_limit'
    elif isinstance(exc, UnexpectedModelBehavior):
        diagnostic['code'] = 'output_validation' if retries or 'retries' in message.lower() else 'invalid_model_response'
    elif page and isinstance(exc, ValueError) and retries:
        diagnostic['code'] = 'output_validation'
    elif isinstance(exc, TimeoutError):
        diagnostic['code'] = 'model_timeout'
    return diagnostic


def failure_message(diagnostic):
    if diagnostic['code'] == 'output_limit' and diagnostic['stage'] != 'page_generation':
        return '模型输出达到本轮上限，回答未完整返回。请缩小问题范围或分批提问后重试。'
    return {
        'output_limit': '模型输出达到本轮上限，内容未完整返回。请精简页面内容后重试；未保存不完整页面。',
        'output_validation': '模型返回的结构或工具参数未通过校验，已停止本轮任务。请重试；不会跳过引用和数据校验。',
        'invalid_model_response': '模型返回了无法解析的响应，已停止本轮任务。请稍后重试。',
        'model_timeout': '模型响应超时，本轮任务已停止，请稍后重试。',
    }.get(diagnostic['code'])
