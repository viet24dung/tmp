import pandas as pd
import datetime
import numpy as np
import openai
import os
import re
import json

# sandbox.py
from RestrictedPython import compile_restricted
from RestrictedPython.Guards import safe_builtins,guarded_iter_unpack_sequence
from  RestrictedPython.Eval import default_guarded_getattr, default_guarded_getitem, default_guarded_getiter
import pandas as pd

class Sandbox:
    def __init__(self):
        self._allowed_imports = {}

    def allow_import(self, module_name):
        try:
            module = __import__(module_name)
            self._allowed_imports[module_name] = module
        except ImportError:
            pass

    def execute(self, code, local_vars = {}):
        allowed_builtins = safe_builtins
        # Add __builtins__, __import__, and allowed imports to the globals
        restricted_globals = {"__builtins__": allowed_builtins}
        restricted_globals.update(self._allowed_imports)

        builtin_mappings = {
            "__import__": __import__,
            "_getattr_": default_guarded_getattr,
            "_getitem_": default_guarded_getitem,
            "_getiter_": default_guarded_getiter,
            "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
            "list": list,
            "set": set,
            "pd": pd,
        }

        series_methods = [
            "sum", "mean", "any", "argmax", "argmin", "count", "cumsum", "cumprod", "diff",
            "dropna", "fillna", "head", "idxmax", "idxmin", "last", "max", "min", "notna",
            "prod", "quantile", "rename", "round", "tail", "to_frame", "to_list", "to_numpy",
            "to_string","unique",  "sort_index", "sort_values", "aggregate"
        ]


        builtin_mappings.update({method: getattr(pd.Series, method) for method in series_methods})

        restricted_globals["__builtins__"].update(builtin_mappings)

        byte_code = compile_restricted(source=code, filename='<inline>', mode='exec')

        # Execute the restricted code
        exec(byte_code, restricted_globals, local_vars)

        return local_vars


class PandasLLM(pd.DataFrame):
    """
    PandasLLM is a subclass of the Pandas DataFrame class. It is designed to provide a
    wrapper around the OpenAI API. 
    """

    code_blocks = [r'```python(.*?)```',r'```(.*?)```']

    llm_default_model = "gpt-3.5-turbo"
    llm_default_temperature = 0.2
    llm_engine = "openai"
    llm_default_params = { "model": llm_default_model,
                            "temperature": llm_default_temperature}
    llm_api_key = None
    
    prompt_override = False
    custom_prompt = ""
    data_privacy = True
    path = None
    verbose = False 
    code_block = ""
    force_sandbox = False
    def __init__(self, 
                 data, 
                 llm_engine:str = "openai", llm_params=llm_default_params, 
                 prompt_override:bool = False,     
                 custom_prompt:str = "", 
                 path:str = None,
                 verbose:bool = False,
                 data_privacy:bool = True,
                 llm_api_key:str = None,
                 force_sandbox:bool = False,
                 *args, **kwargs):
        """
        This is the constructor for the PandasLLM class. It takes in the following arguments:
        data: The data to be used. It can be a Pandas DataFrame, a list of lists, a list of tuples,
        a list of dictionaries, a dictionary, a string, or a list.
        llm_engine: The name of the OpenAI engine to use.
        llm_params: A dictionary of parameters to be used with the OpenAI API.
        prompt_override: A boolean that determines whether or not the prompt is overridden.
        custom_prompt: A string that overrides the prompt.
        path: The path to the file to be used.
        verbose: A boolean that determines whether or not the output is verbose.
        data_privacy: A boolean that determines whether or not the data is private.
        llm_api_key: The OpenAI API key to be used.
        force_sandbox: if False and the sandbox fails, it will retry using eval (less safe)

        The constructor also calls the parent class's constructor.

        
        Args:
            data (pandas dataframe, mandatory): dataset to query. Defaults to None.
            llm_engine (str, optional): LLM engine, currently only OpenAI is supported. Defaults to "openai".
            llm_params (dict, optional): LLM engine parameters. Defaults to model=gpt-3.5-turbo and temperature=0.2".
            prompt_override (bool, optional): if True, the custom prompt is mandatory and it will became the main prompt. Defaults to False.
            custom_prompt (str, optional): if prompt_override is False, the custom prompt will be added to the default pandas_llm prompt. Defaults to "".
            path (str, optional): the path where the files containing debug data will be save. Defaults to None.
            verbose (bool, optional): if True debugging info will be printed. Defaults to False.
            data_privacy (bool, optional): if True, the function will not send the data content to OpenAI. Defaults to True.
            llm_api_key (str, optional): the Open API key. Defaults to None.
            force_sandbox (bool, optional): if False and the sandbox fails, it will retry using eval (less safe). Defaults to False.
        """


        super().__init__(data, *args, **kwargs)
        
        self.llm_params = llm_params or {}

        # Set up OpenAI API key from the environment or the config
        self.llm_api_key = llm_api_key or os.environ.get("OPENAI_API_KEY")

        self.llm_engine = llm_engine
        self.llm_params = llm_params or {}
        self.model = self.llm_params.get("model", self.llm_default_model)
        self.temperature = self.llm_params.get("temperature", self.llm_default_temperature)

        self.prompt_override = prompt_override
        self.custom_prompt = custom_prompt

        self.data_privacy = data_privacy
        self.path = path
        self.verbose = verbose
        self.force_sandbox = force_sandbox

    def _buildPromptForRole(self):
        prompt_role = f"""
I want you to act as a data scientist and Python coder. I want you code for me. 
I have a dataset of {len(self)} rows and {len(self.columns)} columns.
Columns and their type are the following:
        """

        for col in self.columns:
            col_type = self.dtypes[col]
            prompt_role += f"{col} ({col_type})\n"
        return prompt_role

    def _buildPromptForProblemSolving(self, request):

        if self.prompt_override:
            return self.custom_prompt

        columns = ""
        for col in self.columns:
            col_type = self.dtypes[col]
            columns += f"{col} ({col_type})\n"

        prompt_problem = f"""
Given a DataFrame named 'df' of {len(self)} rows and {len(self.columns)} columns,
Its columns are the following:

{columns}

I want you to solve the following problem:
write a Python code snippet that addresses the following request:
{request}

While crafting the code, please follow these guidelines:
1. When comparing or searching for strings, use lower case letters, ignore case sensitivity, and apply a "contains" search.
2. Ensure that the answer is a single line of code without explanations, comments, or additional details. 
3. If a single line solution is not possible, multiline solutions or functions are acceptable, but the code must end with an assignment to the variable 'result'.
4. Assign the resulting code to the variable 'result'.
5. Avoid importing any additional libraries than pandas and numpy.

"""
        if not self.custom_prompt is None and len(self.custom_prompt) > 0:
             
            prompt_problem += f"""
            Also:
            {self.custom_prompt}
            """

        return prompt_problem

    def _extractPythonCode(self, text: str, regexp: str) -> str:
        # Define the regular expression pattern for the Python code block
        pattern = regexp
        
        # Search for the pattern in the input text
        match = re.search(pattern, text, re.DOTALL)
        
        # If a match is found, return the extracted code (without the markers)
        if match:
            return match.group(1).strip()
        
        # If no match is found, return an empty string
        return ""

    def _print(self,  *args, **kwargs):
        if self.verbose:
            print(*args, **kwargs)

    # def _variable_to_string(self, variable):
    #     if variable is None: return None
    #     try:

    #         if isinstance(variable, pd.Series):
    #             # convert to dataframe
    #             variable = variable.to_frame()

    #         if isinstance(variable, pd.DataFrame):
    #             variable = variable.drop_duplicates()
    #             if len(variable) == 0: return None
    #             return str(variable)

    #         elif isinstance(variable, np.ndarray):
    #             if len(variable) == 0: return None
    #             return  np.array2string(variable)
    #         else:
    #             # Convert the variable to a string
    #             return str(variable)
    #     except Exception as e:
    #         return str(variable)
        

    def _save(self,name,value):
        if self.path is None or self.path == "":
            return  
        try:
            with open(f"{self.path}/{name}", 'w') as file:
                file.write(value)
        except Exception as e:
            self._print(f"error {e}")
        return

    def _execInSandbox(self, df, generated_code:str):

        # Create a Sandbox instance and allow pandas to be imported
        sandbox = Sandbox()
        sandbox.allow_import("pandas")
        sandbox.allow_import("numpy")

        # Define the initial code to set up the DataFrame
        initial_code = f"""
import pandas as pd
import datetime
from pandas import Timestamp
import numpy as np

        """

        # Combine the initial code and the generated code
        full_code = initial_code + "\n" + generated_code
        # print(full_code)
        # exit()
        self._save("prompt_code.py",full_code)
        # Execute the combined code in the Sandbox
        sandbox_result = sandbox.execute(full_code, {"df":df})

        # Get the result from the local_vars dictionary
        result = sandbox_result.get("result")
        return result

    def prompt(self, request: str):
        """

        Args:
            request (str): prompt containing the request. it must be expressed as a question or a problem to solve

        Returns:
            Any: contains the result or solution of the problem. Tipically the result data type is a dataframe, a Series or a float
        """
        
        # Set up OpenAI API key
        openai.api_key = self.llm_api_key

        messages=[
                {"role": "system", 
                "content": self._buildPromptForRole()},
                {"role": "user", 
                "content": self._buildPromptForProblemSolving(request)
                }
            ]

        response = None
        for times in range(0,3):
            try:
                response = openai.ChatCompletion.create(
                model=self.model,
                temperature=self.temperature,
                messages = messages
                )
                break;
            except Exception as e:
                self._print(f"error {e}")
                continue

        if response is None:
            return "Please try later"

        self._save("prompt_cmd.json",json.dumps(messages, indent=4))

        generated_code = response.choices[0].message.content

        if generated_code == "" or generated_code is None:
            self.code_block = ""
            return None
        
        self.code_block = generated_code

        results=[]
        for regexp in self.code_blocks:
            cleaned_code = self._extractPythonCode(generated_code,regexp)
            if cleaned_code == "" or cleaned_code is None:
                continue
            results.append(cleaned_code)
        results.append(generated_code)

        if len(results) == 0:
            return None

        result = None
        for cleaned_code in results:
    
            try:
                result = self._execInSandbox(self, cleaned_code)
            except Exception as e:
                self._print(f"error {e}")
                if not self.force_sandbox:
                    try:
                        expression = re.sub(r"^\s*result\s*=", "", cleaned_code).strip()
                        result = eval(expression, {'df': self, 'pd': pd, 'np': np, 'datetime': datetime, 'result': result})
                    except Exception as e:
                        self._print(f"error {e}")
                        pass

            if result is not None and str(result) != "":
                break

        if self.data_privacy == True:
            # non formatted result
            return result
        
        # currently the privacy option is not needed.
        # in the future, we can choose to send data to LLM if privacy is set to false

        return result


def main(df):
    api_key = ""
    os.environ["OPENAI_API_KEY"] = api_key

    conv_df = PandasLLM(data=df, llm_api_key = os.environ.get("OPENAI_API_KEY"))
    print()
    banner = """
    Welcome to the Donation Data CLI.
    The donation dataset has three columns (name, age, donation)
    Please note that these names, ages, and donations are randomly generated and do not correspond to real individuals or their donations.
    
    You can ask questions like:
    - show me the list of names
    - What is the average age of people who donated?
    - What is the average donation amount?
    - What is the average donation of people older than 30?
    - What is the average donation of people older than 30 who donated more than $50?
    """
    print(banner)

    prompt = "ai là người có mail là cuong.buiviet@vti.com.vn  và ở đơn vị D9"

    result = conv_df.prompt(prompt)
    code = conv_df.code_block
    print(f"Executing the following expression of type {type(result)}:\n{code}\n\nResult is:\n {result}\n")
        

if __name__ == "__main__":
    df = pd.read_excel("/home/vti/Downloads/Đăng ký phòng ở .xlsx")
    df = df.applymap(lambda x: x.lower() if isinstance(x, str) else x)
    df = df.fillna('')

    main(df)
