import logging

from fastmcp.prompts import prompt

logger = logging.getLogger(__name__)

async def table_deep_dive_prompt_impl(
    table_name: str,
    analysis_type: str = "statistical",
    sample_size: int = 100
) -> str:
    """
    Generate a detailed analysis prompt for a specific table.
    
    Args:
        table_name: Name of the table to analyze
        analysis_type: Type of analysis (statistical, data_quality)
        sample_size: Suggested sample size for data exploration
        
    Returns:
        str: The generated table analysis prompt
    """

    analysis_instructions = {
        "statistical": """
        Focus on statistical analysis:
        - Descriptive statistics for numerical columns
        - Data patterns and distributions
        - Temporal trends (if time-based data)
        - Variance and data spread characteristics
        """,
        "data_quality": """
        Focus on data quality assessment:
        - Completeness and missing data patterns
        - Data consistency
        - Duplicate detection and uniqueness
        - Format issues
        """
    }
        

    try:

        analysis_instruction = analysis_instructions.get(analysis_type, analysis_instructions["statistical"])
        
        prompt = f"""
You are a data analyst conducting an in-depth analysis of the table: {table_name}

First, examine the table structure and sample data to understand its content and characteristics.
Use the table-specific resources to get detailed information about this table.
Use kdbx_sql_query_guidance resource for query syntax.

{analysis_instruction.strip()}

Structure your analysis as follows:

1. **Table Overview**: 
   - Business purpose and context of this table
   - Key entity or concept it represents per column
   - Total record count and data volume

2. **Data Profile**:
   - Sample data examination (suggest using LIMIT {sample_size})
   - Unique value counts for categorical fields
   - Range analysis for numerical fields

3. **Temporal Analysis** (if applicable):
   - Time range coverage
   - Data freshness and update patterns  
   - Seasonal or trend patterns
   - Data gaps or irregularities

Focus on actionable insights that would help someone understand and effectively use this data for analysis or decision-making.

Table to analyze: {table_name}
Analysis type: {analysis_type.replace('_', ' ').title()}
        """.strip()
        
        logger.info(f"Generated table deep dive prompt for {table_name}")
        return prompt
        
    except Exception as e:
        logger.error(f"Error generating table analysis prompt: {e}")
        return f"Error generating prompt: {str(e)}"


# Standalone @prompt decorator (FastMCP 3.x native discovery): attaches __fastmcp__ metadata and
# returns the function unchanged. Bare prompt name — composed as `kdbx_table_analysis` via
# mount(namespace="kdbx").
@prompt
async def table_analysis(
    table_name: str,
    analysis_type: str = "statistical",
    sample_size: int = 100
) -> str:
    """
    Conduct detailed analysis of a specific table.
    Analysis_type Options: statistical, data_quality.
    """
    return await table_deep_dive_prompt_impl(table_name, analysis_type, sample_size)