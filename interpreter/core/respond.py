import json
import re
import traceback

import litellm

from ..terminal_interface.utils.display_markdown_message import display_markdown_message
from .render_message import render_message


def respond(interpreter):
    """
    Yields chunks.
    Responds until it decides not to run any more code or say anything else.
    """

    last_unsupported_code = ""
    insert_loop_message = False

    while True:
        ## RENDER SYSTEM MESSAGE ##

        system_message = interpreter.system_message

        # Add language-specific system messages
        for language in interpreter.computer.terminal.languages:
            if hasattr(language, "system_message"):
                system_message += "\n\n" + language.system_message

        # Add custom instructions
        if interpreter.custom_instructions:
            system_message += "\n\n" + interpreter.custom_instructions

        # Storing the messages so they're accessible in the interpreter's computer
        if interpreter.sync_computer:
            output = interpreter.computer.run(
                "python", f"messages={interpreter.messages}"
            )

        ## Rendering ↓
        rendered_system_message = render_message(interpreter, system_message)
        ## Rendering ↑

        rendered_system_message = {
            "role": "system",
            "type": "message",
            "content": rendered_system_message,
        }

        # Create the version of messages that we'll send to the LLM
        messages_for_llm = interpreter.messages.copy()
        messages_for_llm = [rendered_system_message] + messages_for_llm

        if insert_loop_message:
            messages_for_llm.append(
                {
                    "role": "user",
                    "type": "message",
                    "content": loop_message,
                }
            )
            # Yield two newlines to seperate the LLMs reply from previous messages.
            yield {"role": "assistant", "type": "message", "content": "\n\n"}
            insert_loop_message = False

        ### RUN THE LLM ###

        try:
            for chunk in interpreter.llm.run(messages_for_llm):
                yield {"role": "assistant", **chunk}

        except litellm.exceptions.BudgetExceededError:
            display_markdown_message(
                f"""> Max budget exceeded

                **Session spend:** ${litellm._current_cost}
                **Max budget:** ${interpreter.max_budget}

                Press CTRL-C then run `interpreter --max_budget [higher USD amount]` to proceed.
            """
            )
            break
        # Provide extra information on how to change API keys, if we encounter that error
        # (Many people writing GitHub issues were struggling with this)
        except Exception as e:
            if (
                interpreter.offline == False
                and "auth" in str(e).lower()
                or "api key" in str(e).lower()
            ):
                output = traceback.format_exc()
                raise Exception(
                    f"{output}\n\nThere might be an issue with your API key(s).\n\nTo reset your API key (we'll use OPENAI_API_KEY for this example, but you may need to reset your ANTHROPIC_API_KEY, HUGGINGFACE_API_KEY, etc):\n        Mac/Linux: 'export OPENAI_API_KEY=your-key-here',\n        Windows: 'setx OPENAI_API_KEY your-key-here' then restart terminal.\n\n"
                )
            elif interpreter.offline == False and "not have access" in str(e).lower():
                response = input(
                    f"  You do not have access to {interpreter.llm.model}. You will need to add a payment method and purchase credits for the OpenAI API billing page (different from ChatGPT) to use `GPT-4`.\n\nhttps://platform.openai.com/account/billing/overview\n\nWould you like to try GPT-3.5-TURBO instead? (y/n)\n\n  "
                )
                print("")  # <- Aesthetic choice

                if response.strip().lower() == "y":
                    interpreter.llm.model = "gpt-3.5-turbo-1106"
                    interpreter.llm.context_window = 16000
                    interpreter.llm.max_tokens = 4096
                    interpreter.llm.supports_functions = True
                    display_markdown_message(
                        f"> Model set to `{interpreter.llm.model}`"
                    )
                else:
                    raise Exception(
                        "\n\nYou will need to add a payment method and purchase credits for the OpenAI API billing page (different from ChatGPT) to use GPT-4.\n\nhttps://platform.openai.com/account/billing/overview"
                    )
            elif interpreter.offline and not interpreter.os:
                print(traceback.format_exc())
                raise Exception(
                    "Error occurred. "
                    + str(e)
                )
            else:
                raise

        ### RUN CODE (if it's there) ###

        if interpreter.messages[-1]["type"] == "code":
            if interpreter.verbose:
                print("Running code:", interpreter.messages[-1])

            try:
                # What language/code do you want to run?
                language = interpreter.messages[-1]["format"].lower().strip()
                code = interpreter.messages[-1]["content"]

                if language == "text":
                    # It does this sometimes just to take notes. Let it, it's useful.
                    # In the future we should probably not detect this behavior as code at all.
                    continue

                # Is this language enabled/supported?
                if interpreter.computer.terminal.get_language(language) == None:
                    output = f"`{language}` disabled or not supported."

                    yield {
                        "role": "computer",
                        "type": "console",
                        "format": "output",
                        "content": output,
                    }

                    # Let the response continue so it can deal with the unsupported code in another way. Also prevent looping on the same piece of code.
                    if code != last_unsupported_code:
                        last_unsupported_code = code
                        continue
                    else:
                        break

                # Yield a message, such that the user can stop code execution if they want to
                try:
                    yield {
                        "role": "computer",
                        "type": "confirmation",
                        "format": "execution",
                        "content": {
                            "type": "code",
                            "format": language,
                            "content": code,
                        },
                    }
                except GeneratorExit:
                    # The user might exit here.
                    # We need to tell python what we (the generator) should do if they exit
                    break

                # don't let it import computer — we handle that!
                if interpreter.computer.import_computer_api and language == "python":
                    code = code.replace("import computer\n", "pass\n")
                    code = re.sub(
                        r"import computer\.(\w+) as (\w+)", r"\2 = computer.\1", code
                    )
                    code = re.sub(
                        r"from computer import (.+)",
                        lambda m: "\n".join(
                            f"{x.strip()} = computer.{x.strip()}"
                            for x in m.group(1).split(", ")
                        ),
                        code,
                    )
                    code = re.sub(r"import computer\.\w+\n", "pass\n", code)
                    # If it does this it sees the screenshot twice (which is expected jupyter behavior)
                    if code.split("\n")[-1] in [
                        "computer.display.view()",
                        "computer.display.screenshot()",
                        "computer.view()",
                        "computer.screenshot()",
                    ]:
                        code = code + "\npass"

                # sync up some things (is this how we want to do this?)
                interpreter.computer.verbose = interpreter.verbose
                interpreter.computer.debug = interpreter.debug
                interpreter.computer.emit_images = interpreter.llm.supports_vision

                # sync up the interpreter's computer with your computer
                try:
                    if interpreter.sync_computer and language == "python":
                        computer_dict = interpreter.computer.to_dict()
                        if "_hashes" in computer_dict:
                            computer_dict.pop("_hashes")
                        if computer_dict:
                            computer_json = json.dumps(computer_dict)
                            sync_code = f"""import json\ncomputer.load_dict(json.loads('''{computer_json}'''))"""
                            interpreter.computer.run("python", sync_code)
                except Exception as e:
                    if interpreter.debug:
                        raise
                    print(str(e))
                    print("Continuing...")

                ## ↓ CODE IS RUN HERE

                for line in interpreter.computer.run(language, code, stream=True):
                    yield {"role": "computer", **line}

                ## ↑ CODE IS RUN HERE

                # sync up your computer with the interpreter's computer
                try:
                    if interpreter.sync_computer and language == "python":
                        # sync up the interpreter's computer with your computer
                        result = interpreter.computer.run(
                            "python",
                            "import json\ncomputer_dict = computer.to_dict()\nif computer_dict:\n  if '_hashes' in computer_dict:\n    computer_dict.pop('_hashes')\n  print(json.dumps(computer_dict))",
                        )
                        result = result[-1]["content"]
                        interpreter.computer.load_dict(
                            json.loads(result.strip('"').strip("'"))
                        )
                except Exception as e:
                    if interpreter.debug:
                        raise
                    print(str(e))
                    print("Continuing.")

                # yield final "active_line" message, as if to say, no more code is running. unlightlight active lines
                # (is this a good idea? is this our responsibility? i think so — we're saying what line of code is running! ...?)
                yield {
                    "role": "computer",
                    "type": "console",
                    "format": "active_line",
                    "content": None,
                }

            except:
                yield {
                    "role": "computer",
                    "type": "console",
                    "format": "output",
                    "content": traceback.format_exc(),
                }

        else:
            ## LOOP MESSAGE
            # This makes it utter specific phrases if it doesn't want to be told to "Proceed."

            loop_message = interpreter.loop_message
            if interpreter.os:
                loop_message = loop_message.replace(
                    "If the entire task I asked for is done,",
                    "If the entire task I asked for is done, take a screenshot to verify it's complete, or if you've already taken a screenshot and verified it's complete,",
                )
            loop_breakers = interpreter.loop_breakers

            if (
                interpreter.loop
                and interpreter.messages
                and interpreter.messages[-1].get("role", "").lower() == "assistant"
                and not any(
                    task_status in interpreter.messages[-1].get("content", "")
                    for task_status in loop_breakers
                )
            ):
                # Remove past loop_message messages
                interpreter.messages = [
                    message
                    for message in interpreter.messages
                    if message.get("content", "") != loop_message
                ]
                # Combine adjacent assistant messages, so hopefully it learns to just keep going!
                combined_messages = []
                for message in interpreter.messages:
                    if (
                        combined_messages
                        and message["role"] == "assistant"
                        and combined_messages[-1]["role"] == "assistant"
                        and message["type"] == "message"
                        and combined_messages[-1]["type"] == "message"
                    ):
                        combined_messages[-1]["content"] += "\n" + message["content"]
                    else:
                        combined_messages.append(message)
                interpreter.messages = combined_messages

                # Send model the loop_message:
                insert_loop_message = True

                continue

            # Doesn't want to run code. We're done!
            break

    return
